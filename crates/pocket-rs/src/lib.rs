pub mod config;
pub mod error;
pub mod rds;
pub mod resources;
pub mod secrets;

use error::Result;
use tracing::info;

/// シークレットと AWS リソース情報をすべて環境変数にセットする
///
/// Loco boot 前に呼び出す:
/// ```no_run
/// #[tokio::main]
/// async fn main() {
///     magic_pocket_rs::set_envs().await.unwrap();
///     // Loco boot...
/// }
/// ```
pub async fn set_envs() -> Result<()> {
    set_envs_from_secrets(None).await?;
    set_envs_from_resources(None).await?;
    Ok(())
}

/// シークレットのみ環境変数にセットする（二重実行防止付き）
///
/// POCKET_ENVS_SECRETS_LOADED=true が既にセットされている場合はスキップ
pub async fn set_envs_from_secrets(stage: Option<&str>) -> Result<()> {
    if std::env::var("POCKET_ENVS_SECRETS_LOADED").as_deref() == Ok("true") {
        return Ok(());
    }

    let stage = resolve_stage(stage);
    if stage != "__none__" {
        let pocket_config = config::load_config(&stage)?;
        let data = secrets::get_secrets(&pocket_config).await?;

        info!("Setting {} secret env vars", data.len());
        for (key, value) in &data {
            // SAFETY: Lambda はシングルプロセス環境で起動時に1回のみ呼ばれる
            unsafe {
                std::env::set_var(key, value);
            }
        }
    }

    // RDS / DSQL の接続情報は secrets 経路の一部として設定する
    // (Python の set_envs_from_secrets → _set_rds_database_url → _set_dsql_token
    //  と同じ順序。RDS は managed secret の marker 値 `__rds_runtime__` を実値で
    //  上書きするため、必ず上の secrets 展開より後に呼ぶ)
    rds::set_rds_database_url().await?;
    set_dsql_token().await?;

    // 途中で Err になった場合に再呼び出しで復旧できるよう、フラグは成功後に立てる
    unsafe {
        std::env::set_var("POCKET_ENVS_SECRETS_LOADED", "true");
    }
    Ok(())
}

/// AWS リソース情報のみ環境変数にセットする（二重実行防止付き）
///
/// POCKET_ENVS_AWS_RESOURCES_LOADED=true が既にセットされている場合はスキップ
pub async fn set_envs_from_resources(stage: Option<&str>) -> Result<()> {
    if std::env::var("POCKET_ENVS_AWS_RESOURCES_LOADED").as_deref() == Ok("true") {
        return Ok(());
    }

    let stage = resolve_stage(stage);
    if stage == "__none__" {
        // __none__ でも project_name と region はセットする
        let pocket_config = config::load_config_from_general()?;
        unsafe {
            std::env::set_var("POCKET_PROJECT_NAME", &pocket_config.project_name);
            std::env::set_var("POCKET_REGION", &pocket_config.region);
            std::env::set_var("POCKET_ENVS_AWS_RESOURCES_LOADED", "true");
        }
        return Ok(());
    }

    let pocket_config = config::load_config(&stage)?;
    resources::set_envs_from_resources(&pocket_config).await?;

    // 途中で Err になった場合に再呼び出しで復旧できるよう、フラグは成功後に立てる
    unsafe {
        std::env::set_var("POCKET_ENVS_AWS_RESOURCES_LOADED", "true");
    }
    Ok(())
}

fn resolve_stage(stage: Option<&str>) -> String {
    stage
        .map(|s| s.to_string())
        .unwrap_or_else(|| std::env::var("POCKET_STAGE").unwrap_or_else(|_| "__none__".to_string()))
}

/// DSQL の IAM 認証トークンを再生成し、POCKET_DSQL_TOKEN を最新化して返す
///
/// Python の runtime.refresh_dsql_token() に相当。POCKET_DSQL_TOKEN は cold start で
/// 1 回しか生成されず約 15 分で失効するため、長時間稼働した warm Lambda が新しい
/// 接続を張る直前に本関数を呼ぶと、最新トークンで再接続でき、期限切れトークンに
/// よる認証失敗を避けられる。新規接続には戻り値のトークンをそのまま使うこと
/// (env 経由の受け渡しは他スレッドが env を読む構成では推奨しない)。
/// DSQL 未設定 (POCKET_DSQL_ENDPOINT / POCKET_DSQL_REGION が無い) 場合は
/// Ok(None) を返す。
pub async fn refresh_dsql_token() -> Result<Option<String>> {
    let endpoint = match std::env::var("POCKET_DSQL_ENDPOINT") {
        Ok(v) if !v.is_empty() => v,
        _ => return Ok(None),
    };
    let region = match std::env::var("POCKET_DSQL_REGION") {
        Ok(v) if !v.is_empty() => v,
        _ => return Ok(None),
    };

    let sdk_config = aws_config::load_defaults(aws_config::BehaviorVersion::latest()).await;
    let signer = aws_sdk_dsql::auth_token::AuthTokenGenerator::new(
        aws_sdk_dsql::auth_token::Config::builder()
            .hostname(&endpoint)
            .region(aws_config::Region::new(region))
            .build()
            .map_err(|e| error::PocketError::Dsql(e.to_string()))?,
    );
    let token = signer
        .db_connect_admin_auth_token(&sdk_config)
        .await
        .map_err(|e| error::PocketError::Dsql(e.to_string()))?;

    info!("DSQL auth token generated for {}", endpoint);
    unsafe {
        std::env::set_var("POCKET_DSQL_TOKEN", token.as_str());
    }
    Ok(Some(token.as_str().to_string()))
}

/// POCKET_DSQL_ENDPOINT があれば IAM 認証トークンを生成して POCKET_DSQL_TOKEN にセット
async fn set_dsql_token() -> Result<()> {
    refresh_dsql_token().await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// env はプロセス全体で共有され、Rust のテストは並列実行されるため、
    /// POCKET_DSQL_* を触るテストはこの lock で直列化する。
    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    fn clear_dsql_env() {
        unsafe {
            std::env::remove_var("POCKET_DSQL_ENDPOINT");
            std::env::remove_var("POCKET_DSQL_REGION");
            std::env::remove_var("POCKET_DSQL_TOKEN");
        }
    }

    #[test]
    fn test_refresh_dsql_token_returns_none_without_dsql_env() {
        // DSQL 未設定 (Neon / RDS 等) では AWS を叩かず Ok(None) を返し、
        // POCKET_DSQL_TOKEN も触らないこと (Python の refresh_dsql_token と同じ)。
        // ENV_LOCK を await 越しに保持しないよう block_on で駆動する
        // (#[tokio::test] だと clippy::await_holding_lock に触れる)。
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        clear_dsql_env();
        let rt = tokio::runtime::Runtime::new().unwrap();
        let token = rt.block_on(refresh_dsql_token()).unwrap();
        assert_eq!(token, None);
        assert!(std::env::var("POCKET_DSQL_TOKEN").is_err());
    }

    #[test]
    fn test_refresh_dsql_token_returns_none_without_region() {
        // endpoint だけあって region が無い片肺構成でも None (Python と同じ)
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        clear_dsql_env();
        unsafe {
            std::env::set_var("POCKET_DSQL_ENDPOINT", "example.dsql.ap-northeast-1.on.aws");
        }
        let rt = tokio::runtime::Runtime::new().unwrap();
        let token = rt.block_on(refresh_dsql_token()).unwrap();
        assert_eq!(token, None);
        clear_dsql_env();
    }
}
