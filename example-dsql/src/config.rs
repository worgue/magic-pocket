use std::env;

/// DB 接続元。
/// 環境変数の優先順位: `DATABASE_URL` > `PG_HOST` 系 > `DSQL_HOST` 系。
#[derive(Clone, Debug)]
pub enum DbSource {
    /// 通常の接続 URL (ローカル PostgreSQL 等)
    Direct(String),
    /// Aurora DSQL (IAM 認証。トークンは接続のたびに生成する)
    Dsql {
        host: String,
        user: String,
        region: String,
    },
}

/// 環境変数 → AppConfig (設定は環境変数の直読みに集約する。設定ファイル層は持たない)。
/// Lambda では magic-pocket が SSM/Secrets から環境変数として注入する。
#[derive(Debug, Clone)]
pub struct AppConfig {
    /// DB 接続元。接続系の env が 1 つも無ければ None (DB 無しで起動する)。
    pub db_source: Option<DbSource>,
    /// sqlx のクエリログを出すか (`DEBUG=1` / `DEBUG=true`)
    pub debug: bool,
}

impl AppConfig {
    pub fn from_env() -> Self {
        Self {
            db_source: build_db_source(),
            debug: matches!(env::var("DEBUG").as_deref(), Ok("1") | Ok("true")),
        }
    }
}

fn build_db_source() -> Option<DbSource> {
    // 1. DATABASE_URL 直接指定 (最優先)
    if let Ok(url) = env::var("DATABASE_URL") {
        return Some(DbSource::Direct(url));
    }
    // 2. PG_HOST 系 (通常の PostgreSQL、ローカル開発)
    if let Ok(host) = env::var("PG_HOST") {
        let port = env::var("PG_PORT").unwrap_or_else(|_| "5432".to_string());
        let name = env::var("PG_NAME").unwrap_or_else(|_| "pocket_example_dsql".to_string());
        let user = env::var("PG_USER").unwrap_or_else(|_| "postgres".to_string());
        let password = env::var("PG_PASSWORD").unwrap_or_default();
        let auth = if password.is_empty() {
            user
        } else {
            format!("{user}:{password}")
        };
        return Some(DbSource::Direct(format!(
            "postgres://{auth}@{host}:{port}/{name}"
        )));
    }
    // 3. DSQL_HOST 系 (Aurora DSQL、IAM 認証)。
    //    magic-pocket デプロイでは POCKET_DSQL_ENDPOINT として注入されるので両方見る
    //    (POCKET_DSQL_TOKEN は使わない = トークンはアプリ内生成)
    if let Ok(host) = env::var("DSQL_HOST").or_else(|_| env::var("POCKET_DSQL_ENDPOINT")) {
        let user = env::var("DSQL_USER").unwrap_or_else(|_| "admin".to_string());
        let region = env::var("AWS_REGION")
            .or_else(|_| env::var("POCKET_DSQL_REGION"))
            .unwrap_or_else(|_| "ap-northeast-1".to_string());
        return Some(DbSource::Dsql { host, user, region });
    }
    None
}
