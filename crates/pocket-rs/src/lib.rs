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
