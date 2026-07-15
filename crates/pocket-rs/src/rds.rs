use percent_encoding::{utf8_percent_encode, AsciiSet, NON_ALPHANUMERIC};
use tracing::info;

use crate::error::{PocketError, Result};

/// Python の `urllib.parse.quote(s, safe="")` と同じ集合。
///
/// unreserved (ALPHA / DIGIT / `-` / `.` / `_` / `~`) 以外をすべて percent-encode する。
/// RDS の自動生成パスワードには `/`, `@`, `:`, `?`, `#` 等が含まれうるため、
/// encode を Python 側と 1 文字でもズラすと DATABASE_URL の parse 結果が変わる。
const PASSWORD_ENCODE_SET: &AsciiSet = &NON_ALPHANUMERIC
    .remove(b'-')
    .remove(b'.')
    .remove(b'_')
    .remove(b'~');

/// RDS 認証情報 (sm / ssm) から `DATABASE_URL` を構築して環境変数にセットする。
///
/// Python の `runtime.py:_set_rds_database_url()` と対称。RDS 未設定
/// (POCKET_RDS_* が無い) の構成では何もしない。
///
/// `rds_database_url` type の managed secret は deploy 時に marker 値
/// (`__rds_runtime__`) が入るだけなので、secrets 展開の後に本関数が
/// 実値で上書きする必要がある (呼び出し順は lib.rs 側で担保)。
pub async fn set_rds_database_url() -> Result<()> {
    let secret_string = match read_rds_secret_string().await? {
        Some(s) => s,
        None => return Ok(()), // RDS 以外の構成
    };
    let data: serde_json::Value = serde_json::from_str(&secret_string)?;
    let url = build_database_url(&data)?;
    info!("DATABASE_URL built from RDS credentials");
    // SAFETY: Lambda はシングルプロセス環境で起動時に1回のみ呼ばれる
    unsafe {
        std::env::set_var("DATABASE_URL", &url);
    }
    Ok(())
}

/// RDS 認証情報の JSON 文字列を store (sm / ssm) から取得する。
///
/// `POCKET_RDS_SECRET_STORE = "ssm"` のときは `POCKET_RDS_SSM_PARAM` の
/// SecureString を、それ以外は `POCKET_RDS_SECRET_ARN` の Secrets Manager
/// secret を読む。どちらも未設定なら `None` (= RDS 以外)。
///
/// region を明示せず default (Lambda の AWS_REGION) を使うのは、POCKET_RDS_* が
/// 同一 stack の CFn テンプレートから注入される同 region の値だから (Python 側の
/// `boto3.client("secretsmanager")` と同じ解決)。
async fn read_rds_secret_string() -> Result<Option<String>> {
    if std::env::var("POCKET_RDS_SECRET_STORE").as_deref() == Ok("ssm") {
        let param_name = match non_empty_env("POCKET_RDS_SSM_PARAM") {
            Some(v) => v,
            None => return Ok(None),
        };
        let sdk_config = aws_config::load_defaults(aws_config::BehaviorVersion::latest()).await;
        let client = aws_sdk_ssm::Client::new(&sdk_config);
        let resp = client
            .get_parameter()
            .name(&param_name)
            .with_decryption(true)
            .send()
            .await
            .map_err(|e| {
                PocketError::Ssm(format!(
                    "get_parameter failed for {}: {}",
                    param_name,
                    aws_sdk_ssm::error::DisplayErrorContext(&e)
                ))
            })?;
        // ここに来た時点で RDS 構成は確定しているので、値が無いのは
        // 設定ミス。None を返して黙って DATABASE_URL 未設定にすると
        // 接続時まで原因が分からなくなるため即エラーにする
        let value = resp.parameter().and_then(|p| p.value()).ok_or_else(|| {
            PocketError::Ssm(format!("SSM parameter {} has no value", param_name))
        })?;
        return Ok(Some(value.to_string()));
    }

    let arn = match non_empty_env("POCKET_RDS_SECRET_ARN") {
        Some(v) => v,
        None => return Ok(None),
    };
    let sdk_config = aws_config::load_defaults(aws_config::BehaviorVersion::latest()).await;
    let client = aws_sdk_secretsmanager::Client::new(&sdk_config);
    let resp = client
        .get_secret_value()
        .secret_id(&arn)
        .send()
        .await
        .map_err(|e| {
            PocketError::SecretsManager(format!(
                "get_secret_value failed for {}: {}",
                arn,
                aws_sdk_secretsmanager::error::DisplayErrorContext(&e)
            ))
        })?;
    let value = resp.secret_string().ok_or_else(|| {
        PocketError::SecretsManager(format!("secret {} has no SecretString", arn))
    })?;
    Ok(Some(value.to_string()))
}

/// RDS 認証情報の JSON から `postgres://` URL を組み立てる。
///
/// ManageMasterUserPassword のシークレットには host/port/dbname が含まれない
/// 場合があるため、Lambda 環境変数 (POCKET_RDS_ENDPOINT/PORT/DBNAME) で補完する。
fn build_database_url(data: &serde_json::Value) -> Result<String> {
    let raw_password = data
        .get("password")
        .and_then(json_scalar_to_string)
        .ok_or_else(|| PocketError::Config("RDS secret has no `password` field".into()))?;
    let password = utf8_percent_encode(&raw_password, PASSWORD_ENCODE_SET);
    let username = data
        .get("username")
        .and_then(json_scalar_to_string)
        .unwrap_or_else(|| "postgres".to_string());
    let host = json_truthy_string(data.get("host"))
        .or_else(|| non_empty_env("POCKET_RDS_ENDPOINT"))
        .unwrap_or_default();
    let port = json_truthy_string(data.get("port"))
        .or_else(|| non_empty_env("POCKET_RDS_PORT"))
        .unwrap_or_else(|| "5432".to_string());
    let dbname = json_truthy_string(data.get("dbname"))
        .or_else(|| non_empty_env("POCKET_RDS_DBNAME"))
        .unwrap_or_default();
    Ok(format!(
        "postgres://{username}:{password}@{host}:{port}/{dbname}"
    ))
}

/// JSON scalar を文字列にする (String はそのまま / Number は 10 進表現)。
fn json_scalar_to_string(v: &serde_json::Value) -> Option<String> {
    match v {
        serde_json::Value::String(s) => Some(s.clone()),
        serde_json::Value::Number(n) => Some(n.to_string()),
        _ => None,
    }
}

/// Python の `data.get(key) or <fallback>` 相当。
///
/// 空文字 / 0 は falsy として fallback させる (SM の RDS secret は port を
/// 数値で持つことがあるので Number も受ける)。
fn json_truthy_string(v: Option<&serde_json::Value>) -> Option<String> {
    match v? {
        serde_json::Value::String(s) if !s.is_empty() => Some(s.clone()),
        serde_json::Value::Number(n) if n.as_f64() != Some(0.0) => Some(n.to_string()),
        _ => None,
    }
}

/// 未設定と空文字を同じ「無し」として扱う (Python の `if not value` と対称)。
fn non_empty_env(key: &str) -> Option<String> {
    match std::env::var(key) {
        Ok(v) if !v.is_empty() => Some(v),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// env はプロセス全体で共有され、Rust のテストは並列実行されるため、
    /// POCKET_RDS_* を触るテストはこの lock で直列化する。
    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    /// POCKET_RDS_* / DATABASE_URL を消した状態にする (lock 保持中のみ有効)。
    fn clear_rds_env() {
        unsafe {
            std::env::remove_var("POCKET_RDS_SECRET_STORE");
            std::env::remove_var("POCKET_RDS_SECRET_ARN");
            std::env::remove_var("POCKET_RDS_SSM_PARAM");
            std::env::remove_var("POCKET_RDS_ENDPOINT");
            std::env::remove_var("POCKET_RDS_PORT");
            std::env::remove_var("POCKET_RDS_DBNAME");
            std::env::remove_var("DATABASE_URL");
        }
    }

    // 以下 build_database_url のテストは secret 側に全フィールドを持たせるので
    // env fallback 経路に入らず、ENV_LOCK 不要 (ambient env に影響されない)。
    #[test]
    fn test_build_url_full_secret() {
        let data = serde_json::json!({
            "username": "appuser",
            "password": "simplepass",
            "host": "db.example.com",
            "port": 5432,
            "dbname": "appdb",
        });
        assert_eq!(
            build_database_url(&data).unwrap(),
            "postgres://appuser:simplepass@db.example.com:5432/appdb"
        );
    }

    #[test]
    fn test_build_url_defaults_username_to_postgres() {
        let data = serde_json::json!({
            "password": "pw",
            "host": "h",
            "port": "5432",
            "dbname": "d",
        });
        assert_eq!(
            build_database_url(&data).unwrap(),
            "postgres://postgres:pw@h:5432/d"
        );
    }

    #[test]
    fn test_build_url_percent_encodes_password() {
        // RDS の自動生成パスワードに現れる記号が URL 構造を壊さないこと。
        // Python の urllib.parse.quote(pw, safe="") と同じ出力になる必要がある。
        let data = serde_json::json!({
            "username": "u",
            "password": "p@ss:w/rd?#[]&=+ %",
            "host": "h",
            "port": "5432",
            "dbname": "d",
        });
        let url = build_database_url(&data).unwrap();
        assert_eq!(
            url,
            "postgres://u:p%40ss%3Aw%2Frd%3F%23%5B%5D%26%3D%2B%20%25@h:5432/d"
        );
    }

    #[test]
    fn test_build_url_keeps_unreserved_chars_unencoded() {
        // unreserved (-._~) は Python の quote でも encode されない
        let data = serde_json::json!({
            "username": "u",
            "password": "aA0-._~",
            "host": "h",
            "port": "5432",
            "dbname": "d",
        });
        let url = build_database_url(&data).unwrap();
        assert!(url.starts_with("postgres://u:aA0-._~@"), "got {url}");
    }

    #[test]
    fn test_build_url_requires_password() {
        let data = serde_json::json!({"username": "u", "host": "h"});
        let err = build_database_url(&data).unwrap_err();
        assert!(err.to_string().contains("password"), "got {err}");
    }

    #[test]
    fn test_set_rds_database_url_is_noop_without_rds_env() {
        // RDS 以外の構成 (DSQL / Neon 等) では AWS を一切叩かず DATABASE_URL も
        // 触らないこと。Rust runtime の利用者の大半がこの経路なので、ここで Err に
        // なったり AWS 呼び出しが走ったりすると boot 全体が壊れる。
        // ENV_LOCK を await 越しに保持しないよう block_on で駆動する
        // (#[tokio::test] だと clippy::await_holding_lock に触れる)。
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        clear_rds_env();
        let rt = tokio::runtime::Runtime::new().unwrap();
        rt.block_on(set_rds_database_url()).unwrap();
        assert!(std::env::var("DATABASE_URL").is_err());
    }

    #[test]
    fn test_build_url_falls_back_to_env_when_secret_lacks_fields() {
        // ManageMasterUserPassword の secret は host/port/dbname を含まないことが
        // あり、その場合は CFn が注入する Lambda env で補完する。
        // 期待値は Python 実装 (_set_rds_database_url) の実出力と突き合わせ済み。
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        clear_rds_env();
        unsafe {
            std::env::set_var("POCKET_RDS_ENDPOINT", "envhost");
            std::env::set_var("POCKET_RDS_PORT", "6543");
            std::env::set_var("POCKET_RDS_DBNAME", "envdb");
        }
        let data = serde_json::json!({"username": "u", "password": "pw"});
        assert_eq!(
            build_database_url(&data).unwrap(),
            "postgres://u:pw@envhost:6543/envdb"
        );

        // secret 側が空文字 / 0 でも env へ fallback する (Python の `or` と同じ)
        let data = serde_json::json!({
            "username": "u", "password": "pw", "host": "", "port": 0, "dbname": "",
        });
        assert_eq!(
            build_database_url(&data).unwrap(),
            "postgres://u:pw@envhost:6543/envdb"
        );
        clear_rds_env();
    }

    #[test]
    fn test_build_url_port_defaults_to_5432_without_secret_or_env() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        clear_rds_env();
        let data = serde_json::json!({"username": "u", "password": "pw"});
        // host / dbname は Python 同様 空文字のまま (env も secret も無い場合)
        assert_eq!(build_database_url(&data).unwrap(), "postgres://u:pw@:5432/");
    }

    #[test]
    fn test_json_truthy_string_treats_empty_and_zero_as_absent() {
        // Python の `data.get("host") or os.environ.get(...)` と同じ falsy 判定
        assert_eq!(json_truthy_string(None), None);
        assert_eq!(json_truthy_string(Some(&serde_json::json!(""))), None);
        assert_eq!(json_truthy_string(Some(&serde_json::json!(0))), None);
        assert_eq!(json_truthy_string(Some(&serde_json::json!(null))), None);
        assert_eq!(
            json_truthy_string(Some(&serde_json::json!("x"))),
            Some("x".to_string())
        );
        assert_eq!(
            json_truthy_string(Some(&serde_json::json!(5432))),
            Some("5432".to_string())
        );
    }
}
