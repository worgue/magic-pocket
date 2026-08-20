//! DB 接続層。
//!
//! DSQL は IAM 認証トークン (最大 15 分) を接続確立時のみ使うため、
//! - プールは接続を短命化する (`max_lifetime < トークン期限`)
//! - 常駐プロセス (warm Lambda / サーバー) は [`TokenRefresher`] で
//!   プールの接続オプションを期限前に差し替える (無いと起動 15〜20 分後から
//!   新規接続が全て失効トークンで access denied になり、コンテナ再起動まで全滅する)

use std::sync::Mutex;
use std::time::{Duration, SystemTime};

use sea_orm::{ConnectOptions, Database, DatabaseConnection};

use crate::config::{AppConfig, DbSource};

/// トークン期限 (15分) より十分短く。この閾値を超えたら再生成する
const REFRESH_AFTER: Duration = Duration::from_secs(300);

/// DB 接続。接続系 env が無ければ None (DB 無しで起動する)。
/// 短命プロセス (import / migrate 等の bin) とテストはこれだけで足りる。
pub async fn connect(config: &AppConfig) -> Option<DatabaseConnection> {
    let source = config.db_source.as_ref()?;
    let is_dsql = matches!(source, DbSource::Dsql { .. });

    let url = build_url(source).await;
    let mut opt = ConnectOptions::new(&url);
    opt.max_connections(5).sqlx_logging(config.debug);

    if is_dsql {
        // DSQL: サーバー側アイドルタイムアウト 60分固定 / IAM トークン期限 最大15分。
        // プールが stale 接続を再利用すると access denied になるため、
        // トークン有効期限内に接続をリサイクルし、アイドル接続を積極的に破棄する。
        opt.min_connections(0)
            .idle_timeout(Duration::from_secs(300)) // 5分: アイドル接続を早期に解放
            .max_lifetime(Duration::from_secs(600)); // 10分: トークン期限(15分)内にリサイクル
    } else {
        opt.min_connections(1);
    }

    let db = Database::connect(opt)
        .await
        .expect("データベース接続に失敗しました");
    Some(db)
}

/// 常駐プロセス (サーバー / warm Lambda) 用。DSQL のときだけ refresher を返すので、
/// 最外層 middleware (`app::pre_request_maintenance`) に配線する。
pub async fn connect_with_refresher(
    config: &AppConfig,
) -> (Option<DatabaseConnection>, Option<TokenRefresher>) {
    let db = connect(config).await;
    let refresher = match (&db, &config.db_source) {
        (Some(_), Some(DbSource::Dsql { host, user, region })) => Some(TokenRefresher {
            host: host.clone(),
            user: user.clone(),
            region: region.clone(),
            generated_at: Mutex::new(SystemTime::now()),
        }),
        _ => None,
    };
    (db, refresher)
}

/// プール生成後もリクエスト契機でトークンの鮮度を保つ。
/// プールは生成時の URL (= 生成時のトークン) で新規接続を張り続けるため、
/// 期限前に `Pool::set_connect_options` で差し替える。既存接続には影響しない
/// (IAM 認証は接続確立時のみ)。
pub struct TokenRefresher {
    host: String,
    user: String,
    region: String,
    generated_at: Mutex<SystemTime>,
}

impl TokenRefresher {
    /// リクエスト契機 (最外層 middleware) で呼ぶ。生成から REFRESH_AFTER を
    /// 超えていたらトークンを再生成し、プールの接続オプションを差し替える
    pub async fn ensure_fresh(&self, db: &DatabaseConnection) {
        let needs_refresh = {
            let generated_at = self.generated_at.lock().unwrap();
            // 時計が巻き戻っていたら (elapsed が Err) 安全側 = 再生成。
            // 経過時間判定は SystemTime を使う (Lambda の freeze/thaw では
            // 単調時計 Instant が信頼できない)
            generated_at.elapsed().map_or(true, |e| e >= REFRESH_AFTER)
        };
        if !needs_refresh {
            return;
        }
        let url = dsql_url(&self.host, &self.user, &self.region).await;
        db.get_postgres_connection_pool()
            .set_connect_options(url.parse().expect("接続 URL の解析に失敗しました"));
        *self.generated_at.lock().unwrap() = SystemTime::now();
    }
}

async fn build_url(source: &DbSource) -> String {
    match source {
        DbSource::Direct(url) => url.clone(),
        DbSource::Dsql { host, user, region } => dsql_url(host, user, region).await,
    }
}

async fn dsql_url(host: &str, user: &str, region: &str) -> String {
    let token = generate_dsql_token(host, region).await;
    // トークンは記号を含むため URL エンコードが必須。TLS 必須なので sslmode=require、
    // DB 名は postgres 固定
    let encoded_token = urlencoding::encode(&token);
    format!("postgres://{user}:{encoded_token}@{host}:5432/postgres?sslmode=require")
}

/// IAM 認証トークンを生成する。`connect()` / refresher の呼び出しごとに
/// 新しく生成する (キャッシュして使い回さない)
async fn generate_dsql_token(host: &str, region: &str) -> String {
    let sdk_config = aws_config::defaults(aws_config::BehaviorVersion::latest())
        .region(aws_config::Region::new(region.to_string()))
        .load()
        .await;

    let token_config = aws_sdk_dsql::auth_token::Config::builder()
        .hostname(host)
        .region(aws_types::region::Region::new(region.to_string()))
        .expires_in(900) // 最大15分
        .build()
        .expect("DSQL トークン設定の構築に失敗しました");

    let signer = aws_sdk_dsql::auth_token::AuthTokenGenerator::new(token_config);

    signer
        .db_connect_admin_auth_token(&sdk_config) // admin 用。非 admin は db_connect_auth_token
        .await
        .expect("DSQL 認証トークン生成に失敗しました")
        .to_string()
}
