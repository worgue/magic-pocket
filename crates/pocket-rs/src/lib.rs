pub mod config;
pub mod error;
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
    set_dsql_token().await?;
    Ok(())
}

/// シークレットのみ環境変数にセットする（二重実行防止付き）
///
/// POCKET_ENVS_SECRETS_LOADED=true が既にセットされている場合はスキップ
pub async fn set_envs_from_secrets(stage: Option<&str>) -> Result<()> {
    if std::env::var("POCKET_ENVS_SECRETS_LOADED").as_deref() == Ok("true") {
        return Ok(());
    }
    // SAFETY: Lambda はシングルプロセス環境で起動時に1回のみ呼ばれる
    unsafe {
        std::env::set_var("POCKET_ENVS_SECRETS_LOADED", "true");
    }

    let stage = resolve_stage(stage);
    if stage == "__none__" {
        return Ok(());
    }

    let pocket_config = config::load_config(&stage)?;
    let data = secrets::get_secrets(&pocket_config).await?;

    info!("Setting {} secret env vars", data.len());
    for (key, value) in &data {
        unsafe {
            std::env::set_var(key, value);
        }
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
    unsafe {
        std::env::set_var("POCKET_ENVS_AWS_RESOURCES_LOADED", "true");
    }

    let stage = resolve_stage(stage);
    if stage == "__none__" {
        // __none__ でも project_name と region はセットする
        let pocket_config = config::load_config_from_general()?;
        unsafe {
            std::env::set_var("POCKET_PROJECT_NAME", &pocket_config.project_name);
            std::env::set_var("POCKET_REGION", &pocket_config.region);
        }
        return Ok(());
    }

    let pocket_config = config::load_config(&stage)?;
    resources::set_envs_from_resources(&pocket_config).await?;

    Ok(())
}

fn resolve_stage(stage: Option<&str>) -> String {
    stage
        .map(|s| s.to_string())
        .unwrap_or_else(|| std::env::var("POCKET_STAGE").unwrap_or_else(|_| "__none__".to_string()))
}

/// POCKET_DSQL_ENDPOINT があれば IAM 認証トークンを生成して POCKET_DSQL_TOKEN にセット
async fn set_dsql_token() -> Result<()> {
    let endpoint = match std::env::var("POCKET_DSQL_ENDPOINT") {
        Ok(v) if !v.is_empty() => v,
        _ => return Ok(()),
    };
    let region = match std::env::var("POCKET_DSQL_REGION") {
        Ok(v) if !v.is_empty() => v,
        _ => return Ok(()),
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
    Ok(())
}
