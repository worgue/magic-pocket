//! `pocket-rs resource container` / `image` の読み取り (Python 版 `container_cli.py` の
//! yaml / status / url / secrets list / status-env と `image_cli.py`)。

use std::collections::{BTreeMap, HashMap};

use anyhow::{bail, Result};
use aws_config::SdkConfig;
use magic_pocket_rs::config::{find_toml_path, load_config_from_str};
use magic_pocket_settings::context::{ContainerContext, Context, SecretsContext};
use magic_pocket_settings::model::StoreType;
use serde_json::Value as Json;

use super::pyrepr;
use super::{
    load, resolve_container, ContainerCommand, ContainerTarget, ImageCommand, SecretsCommand,
};
use crate::aws_util::{has_code, regional_config, sdk_error};
use crate::deploy::run::{container_endpoints, open_browser};
use crate::deploy::secret_store::read_managed;
use crate::stack::container::stack_name;
use crate::stack::ops::{cfn_status, stack_outputs, stack_status, ResourceStatus};
use crate::status::report::Evaluator;
use crate::status::resource::Resource;
use crate::status::sink::Sink;

pub async fn run(command: ContainerCommand, sink: &mut dyn Sink) -> Result<()> {
    match command {
        ContainerCommand::Yaml(target) => {
            let (context, sdk) = load(&target.stage.stage).await?;
            let c = container(&context, &target)?;
            let mut evaluator = super::evaluator(&context, &sdk);
            sink.print(&evaluator.container_template(c).await?.body);
            Ok(())
        }
        ContainerCommand::Status(target) => {
            let (context, sdk) = load(&target.stage.stage).await?;
            let c = container(&context, &target)?;
            let mut evaluator = super::evaluator(&context, &sdk);
            let status = evaluator.status(&Resource::Container(c), sink).await?;
            match status {
                ResourceStatus::Completed => sink.success("Container is working!!!"),
                ResourceStatus::NoExist => sink.warning("Container has not created yet."),
                ResourceStatus::Failed => {
                    sink.danger("Container has failed. Please check console.")
                }
                // Python 版は container ではなく stack の状態を出す
                _ => {
                    let description = evaluator.describe(&c.region, &stack_name(c)).await?;
                    let mut stack = cfn_status(description.as_ref())?;
                    if stack == ResourceStatus::Completed {
                        let template = evaluator.container_template(c).await?;
                        stack = stack_status(description.as_ref(), &template)?;
                    }
                    sink.warning(&format!("Container stack status: {stack}"));
                }
            }
            Ok(())
        }
        ContainerCommand::Url { target, openpath } => {
            let (context, sdk) = load(&target.stage.stage).await?;
            let c = container(&context, &target)?;
            let mut evaluator = super::evaluator(&context, &sdk);
            let status = evaluator.status(&Resource::Container(c), sink).await?;
            if status != ResourceStatus::Completed {
                sink.warning("Container is not working.");
                return Ok(());
            }
            let description = evaluator.describe(&c.region, &stack_name(c)).await?;
            let outputs = stack_outputs(description.as_ref()).unwrap_or_default();
            let endpoints = container_endpoints(c, &outputs);
            match endpoints.iter().find(|(key, _)| key == "wsgi") {
                Some((_, endpoint)) => {
                    sink.success(&format!("wsgi url: {endpoint}"));
                    if let Some(path) = openpath {
                        open_browser(&format!("{endpoint}/{path}"));
                    }
                }
                None => sink.warning("wsgi endpoint not found."),
            }
            Ok(())
        }
        ContainerCommand::Secrets {
            command: SecretsCommand::List { stage, show_values },
        } => {
            let (context, sdk) = load(&stage.stage).await?;
            list_secrets(&context, &sdk, show_values, sink).await
        }
        ContainerCommand::StatusEnv { target, handler } => {
            let (context, sdk) = load(&target.stage.stage).await?;
            if context.container.is_empty() {
                bail!("[container.<name>] が設定されていません");
            }
            let c = resolve_container(&context, target.container.as_deref())?;
            for view in c.secrets_views() {
                if !view.managed.is_empty() {
                    log_reading(view, sink);
                }
            }
            let fresh = fresh_secrets(&target.stage.stage, &c.name).await?;
            status_env(c, &sdk, &fresh, handler.as_deref(), sink).await
        }
    }
}

pub async fn run_image(command: ImageCommand) -> Result<()> {
    match command {
        ImageCommand::Repo(target) => {
            let (context, _sdk) = load(&target.stage.stage).await?;
            println!("{}", container(&context, &target)?.ecr_name);
            Ok(())
        }
        ImageCommand::Uri(target) => {
            let (context, sdk) = load(&target.stage.stage).await?;
            let c = container(&context, &target)?;
            let ecr = aws_sdk_ecr::Client::new(&regional_config(&sdk, &c.region));
            let Some(uri) = crate::deploy::ecr::repository_uri(&ecr, &c.ecr_name).await? else {
                bail!(
                    "ECR repository '{}' が見つかりません。先に `pocket deploy` を実行してください。",
                    c.ecr_name
                );
            };
            let Some(digest) = image_digest(&ecr, c).await? else {
                bail!(
                    "tag ':{}' のイメージが '{}' に見つかりません。deploy 済みか確認してください。",
                    target.stage.stage,
                    c.ecr_name
                );
            };
            println!("{uri}@{digest}");
            Ok(())
        }
    }
}

/// `--container` 指定 (単一 container なら省略可) の container (Python の
/// `get_container_resource`)
fn container<'c>(context: &'c Context, target: &ContainerTarget) -> Result<&'c ContainerContext> {
    if context.container.is_empty() {
        bail!("container is not configured for this stage");
    }
    resolve_container(context, target.container.as_deref())
}

/// `:<stage>` tag の image の digest (repository か image が無ければ `None`)
async fn image_digest(ecr: &aws_sdk_ecr::Client, c: &ContainerContext) -> Result<Option<String>> {
    let response = ecr
        .describe_images()
        .repository_name(&c.ecr_name)
        .image_ids(
            aws_sdk_ecr::types::ImageIdentifier::builder()
                .image_tag(&c.stage)
                .build(),
        )
        .send()
        .await;
    match response {
        Ok(response) => Ok(response
            .image_details()
            .first()
            .and_then(|d| d.image_digest())
            .map(str::to_string)),
        Err(e)
            if has_code(
                &e,
                &["RepositoryNotFoundException", "ImageNotFoundException"],
            ) =>
        {
            Ok(None)
        }
        Err(e) => Err(sdk_error("ecr describe_images", &e)),
    }
}

fn store_name(store: StoreType) -> &'static str {
    match store {
        StoreType::Sm => "sm",
        StoreType::Ssm => "ssm",
    }
}

/// (ラベル, store view) の一覧 (shared store + 名前順の各 container の store)
fn secrets_views(context: &Context) -> Vec<(&str, &SecretsContext)> {
    let mut views = Vec::new();
    if let Some(shared) = context.secrets.as_ref() {
        views.push(("shared", shared));
    }
    let mut names: Vec<&String> = context.container.keys().collect();
    names.sort();
    for name in names {
        let c = context.container.get(name).expect("container");
        if let Some(sc) = c.secrets.as_ref() {
            views.push((name.as_str(), sc));
        }
    }
    views
}

/// Python の `print("  - " + str(value))` の `str` (文字列はそのまま、他は repr)
fn py_str(value: &Json) -> String {
    match value {
        Json::String(s) => s.clone(),
        other => pyrepr::repr(other, false),
    }
}

async fn list_secrets(
    context: &Context,
    sdk: &SdkConfig,
    show_values: bool,
    sink: &mut dyn Sink,
) -> Result<()> {
    let views = secrets_views(context);
    if views.is_empty() {
        sink.warning("secrets is not configured for this stage");
        return Ok(());
    }
    for (label, sc) in views {
        sink.info(&format!("[{label}] {}", sc.pocket_key));
        for (key, spec) in sc.user.iter() {
            let store = spec.store.unwrap_or(sc.store);
            let name = spec.name.as_deref().unwrap_or("None");
            sink.print(&format!("{key}: {name} (store={})", store_name(store)));
            if show_values {
                let value = user_secret_value(sdk, &sc.region, store, name).await?;
                sink.print(&format!("  - {value}"));
            }
        }
        // 読むだけ (削除予約中の secret は復元しない)。無ければ空
        let stored = if sc.managed.is_empty() {
            Default::default()
        } else {
            log_reading(sc, sink);
            read_managed(sdk, sc, false).await?.unwrap_or_default()
        };
        for (key, spec) in sc.managed.iter() {
            let created = stored.contains_key(key);
            let kind = serde_json::to_value(spec.r#type)?;
            let options = serde_json::to_value(&spec.options)?;
            sink.print(&format!(
                "{key}: {} {}",
                kind.as_str().unwrap_or_default(),
                pyrepr::repr(&options, false)
            ));
            sink.print(if created {
                "  - CREATED"
            } else {
                "  - NOEXIST"
            });
            if created && show_values {
                match &stored[key] {
                    Json::Object(map) => {
                        for (k, v) in map {
                            sink.print(&format!("  - {k}: {}", py_str(v)));
                        }
                    }
                    other => sink.print(&format!("  - {}", py_str(other))),
                }
            }
        }
    }
    Ok(())
}

/// managed secret を読む前のログ (Python の store 実装と同じ文言)
fn log_reading(view: &SecretsContext, sink: &mut dyn Sink) {
    match view.store {
        StoreType::Sm => sink.log(&format!(
            "Requesting pocket secrets {} ...",
            view.pocket_key
        )),
        StoreType::Ssm => sink.log(&format!(
            "Requesting pocket secrets via SSM {} ...",
            view.pocket_key
        )),
    }
}

/// user secret の値 (SM は SecretString、SSM は復号した値)
async fn user_secret_value(
    sdk: &SdkConfig,
    region: &str,
    store: StoreType,
    name: &str,
) -> Result<String> {
    let config = regional_config(sdk, region);
    match store {
        StoreType::Sm => {
            let response = aws_sdk_secretsmanager::Client::new(&config)
                .get_secret_value()
                .secret_id(name)
                .send()
                .await
                .map_err(|e| sdk_error("secretsmanager get_secret_value", &e))?;
            Ok(response.secret_string().unwrap_or_default().to_string())
        }
        StoreType::Ssm => {
            let response = aws_sdk_ssm::Client::new(&config)
                .get_parameter()
                .name(name)
                .with_decryption(true)
                .send()
                .await
                .map_err(|e| sdk_error("ssm get_parameter", &e))?;
            Ok(response
                .parameter()
                .and_then(|p| p.value())
                .unwrap_or_default()
                .to_string())
        }
    }
}

/// secret の最新値 (Lambda の runtime と同じ展開。Python の `pocket.runtime.get_secrets`)
pub(crate) async fn fresh_secrets(
    stage: &str,
    container: &str,
) -> Result<BTreeMap<String, String>> {
    let toml_path = find_toml_path()?;
    let content = std::fs::read_to_string(&toml_path)?;
    let config = load_config_from_str(&content, stage, Some(container))?;
    let secrets: HashMap<String, String> = magic_pocket_rs::secrets::get_secrets(&config).await?;
    Ok(secrets.into_iter().collect())
}

/// 対象の handler 名 (`--handler` が無ければ全部。Python の `_resolve_lambda_target_handlers`)
pub(crate) fn target_handlers(c: &ContainerContext, handler: Option<&str>) -> Result<Vec<String>> {
    let Some(handler) = handler.filter(|h| !h.is_empty()) else {
        return Ok(c.handlers.keys().cloned().collect());
    };
    if !c.handlers.contains_key(handler) {
        let mut names: Vec<&String> = c.handlers.keys().collect();
        names.sort();
        let names: Vec<&str> = names.iter().map(|s| s.as_str()).collect();
        bail!(
            "handler '{handler}' が見つかりません。利用可能: {}",
            names.join(", ")
        );
    }
    Ok(vec![handler.to_string()])
}

/// Lambda の今の環境変数 (function が無ければ deploy を促して止める)
pub(crate) async fn lambda_env(
    client: &aws_sdk_lambda::Client,
    function_name: &str,
) -> Result<BTreeMap<String, String>> {
    let response = client
        .get_function_configuration()
        .function_name(function_name)
        .send()
        .await;
    let response = match response {
        Ok(response) => response,
        Err(e) if has_code(&e, &["ResourceNotFoundException"]) => bail!(
            "Lambda function '{function_name}' が見つかりません。先に `pocket deploy` を実行してください。"
        ),
        Err(e) => return Err(sdk_error("lambda get_function_configuration", &e)),
    };
    Ok(response
        .environment()
        .and_then(|env| env.variables())
        .map(|vars| vars.iter().map(|(k, v)| (k.clone(), v.clone())).collect())
        .unwrap_or_default())
}

/// 宣言値と違う (Lambda に無いか古い) key
pub(crate) fn drift_keys(
    current: &BTreeMap<String, String>,
    fresh: &BTreeMap<String, String>,
) -> Vec<String> {
    fresh
        .iter()
        .filter(|(k, v)| current.get(*k) != Some(*v))
        .map(|(k, _)| k.clone())
        .collect()
}

async fn status_env(
    c: &ContainerContext,
    sdk: &SdkConfig,
    fresh: &BTreeMap<String, String>,
    handler: Option<&str>,
    sink: &mut dyn Sink,
) -> Result<()> {
    let client = aws_sdk_lambda::Client::new(&regional_config(sdk, &c.region));
    let mut any_drift = false;
    for name in target_handlers(c, handler)? {
        let function_name = &c.handlers.get(&name).expect("handler").function_name;
        let current = lambda_env(&client, function_name).await?;
        let drift = drift_keys(&current, fresh);
        print_handler_drift(&name, fresh.len(), &current, &drift, sink);
        any_drift |= !drift.is_empty();
    }
    if any_drift {
        sink.warning("drift があります。`pocket resource container reload-env` で同期できます。");
    } else {
        sink.success("drift なし。Lambda env と secrets は同期されています。");
    }
    Ok(())
}

fn print_handler_drift(
    name: &str,
    declared: usize,
    current: &BTreeMap<String, String>,
    drift: &[String],
    sink: &mut dyn Sink,
) {
    sink.info(&format!(
        "[{name}] secret keys: {declared} declared, drift: {}",
        drift.len()
    ));
    for key in drift {
        if current.contains_key(key) {
            sink.warning(&format!("  ~ {key} (Lambda 値が古い、reload-env で更新)"));
        } else {
            sink.warning(&format!("  + {key} (Lambda に未反映、reload-env で投入)"));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stack::testing::vector_context;
    use crate::status::sink::{Recorded, Stream, Style};

    fn map(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    #[test]
    fn test_drift_lines() {
        let current = map(&[("A", "1"), ("B", "old"), ("X", "keep")]);
        let fresh = map(&[("A", "1"), ("B", "new"), ("C", "3")]);
        let drift = drift_keys(&current, &fresh);
        assert_eq!(drift, ["B", "C"]);
        let mut sink = Recorded::default();
        print_handler_drift("wsgi", fresh.len(), &current, &drift, &mut sink);
        assert_eq!(
            sink.0,
            [
                (
                    Stream::Stderr,
                    Style::Info,
                    "[wsgi] secret keys: 3 declared, drift: 2".to_string()
                ),
                (
                    Stream::Stderr,
                    Style::Warning,
                    "  ~ B (Lambda 値が古い、reload-env で更新)".to_string()
                ),
                (
                    Stream::Stderr,
                    Style::Warning,
                    "  + C (Lambda に未反映、reload-env で投入)".to_string()
                ),
            ]
        );
    }

    #[test]
    fn test_target_handlers() {
        let ctx = vector_context("toml/rds.dev");
        let c = ctx.container.values().next().unwrap();
        let all = target_handlers(c, None).unwrap();
        assert_eq!(all, c.handlers.keys().cloned().collect::<Vec<_>>());
        let first = all[0].clone();
        assert_eq!(target_handlers(c, Some(&first)).unwrap(), [first]);
        let error = target_handlers(c, Some("nope")).unwrap_err().to_string();
        assert!(
            error.starts_with("handler 'nope' が見つかりません。利用可能: "),
            "{error}"
        );
    }

    #[test]
    fn test_py_str() {
        assert_eq!(py_str(&serde_json::json!("v")), "v");
        assert_eq!(py_str(&serde_json::json!(true)), "True");
        assert_eq!(py_str(&serde_json::json!({"a": 1})), "{'a': 1}");
    }
}
