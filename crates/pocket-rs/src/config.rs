use std::collections::HashMap;
use std::path::{Path, PathBuf};

use serde::Deserialize;

use crate::error::{PocketError, Result};

/// pocket.toml から読み取った設定（ステージマージ済み）
#[derive(Debug, Clone)]
pub struct PocketConfig {
    pub region: String,
    pub project_name: String,
    pub namespace: String,
    pub prefix_template: String,
    pub stage: String,
    pub slug: String,
    pub resource_prefix: String,
    pub secrets: Option<SecretsConfig>,
    pub handlers: HashMap<String, HandlerConfig>,
}

#[derive(Debug, Clone)]
pub struct SecretsConfig {
    pub store: StoreType,
    pub pocket_key: String,
    pub stage: String,
    pub project_name: String,
    pub region: String,
    pub managed: HashMap<String, ManagedSecretSpec>,
    pub user: HashMap<String, UserSecretSpec>,
}

#[derive(Debug, Clone, PartialEq)]
pub enum StoreType {
    Sm,
    Ssm,
}

#[derive(Debug, Clone)]
pub struct ManagedSecretSpec {
    pub secret_type: String,
    pub options: HashMap<String, String>,
}

#[derive(Debug, Clone)]
pub struct UserSecretSpec {
    pub name: String,
    pub store: Option<StoreType>,
}

#[derive(Debug, Clone)]
pub struct HandlerConfig {
    pub apigateway: Option<ApiGatewayConfig>,
    pub sqs: Option<SqsConfig>,
}

#[derive(Debug, Clone)]
pub struct ApiGatewayConfig {
    pub domain: Option<String>,
}

#[derive(Debug, Clone)]
pub struct SqsConfig {
    pub name: String,
}

// --- TOML の中間デシリアライズ型 ---

#[derive(Debug, Deserialize)]
struct GeneralToml {
    region: String,
    project_name: Option<String>,
    #[serde(default = "default_namespace")]
    namespace: String,
    #[serde(default = "default_prefix_template")]
    prefix_template: String,
    #[allow(dead_code)]
    stages: Vec<String>,
}

fn default_namespace() -> String {
    "pocket".to_string()
}

fn default_prefix_template() -> String {
    "{stage}-{project}-{namespace}-".to_string()
}

#[derive(Debug, Deserialize)]
struct AwsContainerToml {
    secrets: Option<SecretsToml>,
    #[serde(default)]
    handlers: HashMap<String, HandlerToml>,
}

#[derive(Debug, Deserialize)]
struct SecretsToml {
    #[serde(default = "default_store")]
    store: String,
    #[serde(default = "default_pocket_key_format")]
    pocket_key_format: String,
    #[serde(default)]
    managed: HashMap<String, ManagedSecretToml>,
    #[serde(default)]
    user: HashMap<String, UserSecretToml>,
}

fn default_store() -> String {
    "sm".to_string()
}

fn default_pocket_key_format() -> String {
    "{stage}-{project}-{namespace}".to_string()
}

#[derive(Debug, Deserialize)]
struct ManagedSecretToml {
    #[serde(rename = "type")]
    secret_type: String,
    #[serde(default)]
    options: HashMap<String, toml::Value>,
}

#[derive(Debug, Deserialize)]
struct UserSecretToml {
    name: String,
    store: Option<String>,
}

#[derive(Debug, Deserialize)]
struct HandlerToml {
    apigateway: Option<ApiGatewayToml>,
    sqs: Option<toml::Value>,
    #[serde(default)]
    #[allow(dead_code)]
    timeout: Option<u64>,
}

#[derive(Debug, Deserialize)]
struct ApiGatewayToml {
    domain: Option<String>,
}

// --- パブリック関数 ---

/// CWD から上方向に pocket.toml を探す
pub fn find_toml_path() -> Result<PathBuf> {
    let mut current = std::env::current_dir().map_err(PocketError::Io)?;
    loop {
        let candidate = current.join("pocket.toml");
        if candidate.exists() {
            return Ok(candidate);
        }
        if !current.pop() {
            return Err(PocketError::TomlNotFound);
        }
    }
}

/// pocket.toml をパースして PocketConfig を返す
pub fn load_config(stage: &str) -> Result<PocketConfig> {
    let toml_path = find_toml_path()?;
    load_config_from_path(&toml_path, stage)
}

/// pocket.toml から general セクションのみ読み取る（stage 不要のケース用）
pub fn load_config_from_general() -> Result<PocketConfig> {
    let toml_path = find_toml_path()?;
    let content = std::fs::read_to_string(&toml_path).map_err(PocketError::Io)?;
    let data: toml::Value = content.parse().map_err(PocketError::TomlParse)?;

    let general: GeneralToml = {
        let general_val = data
            .get("general")
            .ok_or_else(|| PocketError::Config("missing [general] section".into()))?;
        general_val.clone().try_into().map_err(PocketError::TomlParse)?
    };

    let project_name = general.project_name.unwrap_or_else(|| "unknown".to_string());

    Ok(PocketConfig {
        region: general.region,
        project_name,
        namespace: general.namespace,
        prefix_template: general.prefix_template,
        stage: String::new(),
        slug: String::new(),
        resource_prefix: String::new(),
        secrets: None,
        handlers: HashMap::new(),
    })
}

/// 指定パスの pocket.toml をパースして PocketConfig を返す
pub fn load_config_from_path(path: &Path, stage: &str) -> Result<PocketConfig> {
    let content = std::fs::read_to_string(path).map_err(PocketError::Io)?;
    load_config_from_str(&content, stage)
}

/// TOML 文字列から PocketConfig を構築する
pub fn load_config_from_str(content: &str, stage: &str) -> Result<PocketConfig> {
    let mut data: toml::Value = content.parse().map_err(PocketError::TomlParse)?;

    // ステージが stages に含まれるか検証
    let stages = data
        .get("general")
        .and_then(|g| g.get("stages"))
        .and_then(|s| s.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(String::from))
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();

    if !stages.contains(&stage.to_string()) {
        return Err(PocketError::StageNotFound(stage.to_string()));
    }

    // ステージマージ: data[stage] を data に deep merge
    if let Some(stage_data) = data.get(stage).cloned() {
        deep_merge(&mut data, &stage_data);
    }

    // ステージキーを削除
    if let Some(table) = data.as_table_mut() {
        for s in &stages {
            table.remove(s.as_str());
        }
    }

    // general セクションのデシリアライズ
    let general: GeneralToml = {
        let general_val = data
            .get("general")
            .ok_or_else(|| PocketError::Config("missing [general] section".into()))?;
        general_val.clone().try_into().map_err(PocketError::TomlParse)?
    };

    let project_name = general
        .project_name
        .unwrap_or_else(|| "unknown".to_string());

    let format_vars = |s: &str| -> String {
        s.replace("{stage}", stage)
            .replace("{project}", &project_name)
            .replace("{namespace}", &general.namespace)
    };

    let resource_prefix = format_vars(&general.prefix_template);
    let slug = format!("{}-{}", stage, project_name);

    // awscontainer セクション
    let (secrets_config, handlers_config) = if let Some(ac_val) = data.get("awscontainer") {
        let ac: AwsContainerToml = ac_val.clone().try_into().map_err(PocketError::TomlParse)?;

        let secrets_config = ac.secrets.map(|sc| {
            let pocket_key = format_vars(&sc.pocket_key_format);
            let store = parse_store_type(&sc.store);
            let managed = sc
                .managed
                .into_iter()
                .map(|(k, v)| {
                    let options = v
                        .options
                        .into_iter()
                        .map(|(ok, ov)| {
                            let s = match ov {
                                toml::Value::String(s) => s,
                                other => other.to_string(),
                            };
                            (ok, s)
                        })
                        .collect();
                    (
                        k,
                        ManagedSecretSpec {
                            secret_type: v.secret_type,
                            options,
                        },
                    )
                })
                .collect();
            let user = sc
                .user
                .into_iter()
                .map(|(k, v)| {
                    (
                        k,
                        UserSecretSpec {
                            name: v.name,
                            store: v.store.as_deref().map(parse_store_type),
                        },
                    )
                })
                .collect();

            SecretsConfig {
                store,
                pocket_key,
                stage: stage.to_string(),
                project_name: project_name.clone(),
                region: general.region.clone(),
                managed,
                user,
            }
        });

        let handlers_config: HashMap<String, HandlerConfig> = ac
            .handlers
            .into_iter()
            .map(|(key, h)| {
                let apigateway = h.apigateway.map(|ag| ApiGatewayConfig {
                    domain: ag.domain,
                });
                let sqs = if h.sqs.is_some() {
                    let queue_name = format!("{}{}", resource_prefix, key);
                    Some(SqsConfig { name: queue_name })
                } else {
                    None
                };
                (key, HandlerConfig { apigateway, sqs })
            })
            .collect();

        (secrets_config, handlers_config)
    } else {
        (None, HashMap::new())
    };

    Ok(PocketConfig {
        region: general.region,
        project_name,
        namespace: general.namespace,
        prefix_template: general.prefix_template,
        stage: stage.to_string(),
        slug,
        resource_prefix,
        secrets: secrets_config,
        handlers: handlers_config,
    })
}

fn parse_store_type(s: &str) -> StoreType {
    match s {
        "ssm" => StoreType::Ssm,
        _ => StoreType::Sm,
    }
}

/// toml::Value に対する再帰的 deep merge
/// source の値を target に上書きマージする
fn deep_merge(target: &mut toml::Value, source: &toml::Value) {
    match (target, source) {
        (toml::Value::Table(ref mut t_map), toml::Value::Table(s_map)) => {
            for (key, s_val) in s_map {
                if let Some(t_val) = t_map.get_mut(key) {
                    deep_merge(t_val, s_val);
                } else {
                    t_map.insert(key.clone(), s_val.clone());
                }
            }
        }
        (target, source) => {
            *target = source.clone();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MINIMAL_TOML: &str = r#"
[general]
region = "ap-northeast-1"
project_name = "myapp"
stages = ["dev", "prod"]

[awscontainer]
dockerfile_path = "Dockerfile"

[awscontainer.secrets]
store = "ssm"
pocket_key_format = "{stage}-{project}-{namespace}"

[awscontainer.secrets.managed]
SECRET_KEY = { type = "password", options = { length = "50" } }
DATABASE_URL = { type = "neon_database_url" }

[awscontainer.secrets.user]
EXTERNAL_API_KEY = { name = "my-external-key", store = "sm" }

[awscontainer.handlers.wsgi]
command = "handler.wsgi"

[awscontainer.handlers.wsgi.apigateway]
domain = "api.example.com"

[awscontainer.handlers.worker]
command = "handler.worker"
timeout = 600
sqs = {}
"#;

    const STAGE_OVERRIDE_TOML: &str = r#"
[general]
region = "ap-northeast-1"
project_name = "myapp"
stages = ["dev", "prod"]

[awscontainer]
dockerfile_path = "Dockerfile"

[awscontainer.handlers.wsgi]
command = "handler.wsgi"

[dev.awscontainer.handlers.wsgi]
apigateway = {}

[prod.awscontainer.handlers.wsgi.apigateway]
domain = "api.example.com"
"#;

    #[test]
    fn test_load_basic_config() {
        let config = load_config_from_str(MINIMAL_TOML, "dev").unwrap();
        assert_eq!(config.region, "ap-northeast-1");
        assert_eq!(config.project_name, "myapp");
        assert_eq!(config.namespace, "pocket");
        assert_eq!(config.stage, "dev");
        assert_eq!(config.slug, "dev-myapp");
        assert_eq!(config.resource_prefix, "dev-myapp-pocket-");
    }

    #[test]
    fn test_secrets_config() {
        let config = load_config_from_str(MINIMAL_TOML, "dev").unwrap();
        let secrets = config.secrets.unwrap();
        assert_eq!(secrets.store, StoreType::Ssm);
        assert_eq!(secrets.pocket_key, "dev-myapp-pocket");
        assert_eq!(secrets.managed.len(), 2);
        assert!(secrets.managed.contains_key("SECRET_KEY"));
        assert!(secrets.managed.contains_key("DATABASE_URL"));
        assert_eq!(secrets.user.len(), 1);
        let ext = &secrets.user["EXTERNAL_API_KEY"];
        assert_eq!(ext.name, "my-external-key");
        assert_eq!(ext.store, Some(StoreType::Sm));
    }

    #[test]
    fn test_handlers_config() {
        let config = load_config_from_str(MINIMAL_TOML, "dev").unwrap();
        assert_eq!(config.handlers.len(), 2);

        let wsgi = &config.handlers["wsgi"];
        assert!(wsgi.apigateway.is_some());
        assert_eq!(
            wsgi.apigateway.as_ref().unwrap().domain,
            Some("api.example.com".to_string())
        );
        assert!(wsgi.sqs.is_none());

        let worker = &config.handlers["worker"];
        assert!(worker.apigateway.is_none());
        assert!(worker.sqs.is_some());
        assert_eq!(worker.sqs.as_ref().unwrap().name, "dev-myapp-pocket-worker");
    }

    #[test]
    fn test_stage_not_found() {
        let err = load_config_from_str(MINIMAL_TOML, "staging").unwrap_err();
        assert!(matches!(err, PocketError::StageNotFound(_)));
    }

    #[test]
    fn test_stage_merge() {
        let config = load_config_from_str(STAGE_OVERRIDE_TOML, "dev").unwrap();
        let wsgi = &config.handlers["wsgi"];
        // dev ステージは apigateway = {} なので domain なし
        assert!(wsgi.apigateway.is_some());
        assert!(wsgi.apigateway.as_ref().unwrap().domain.is_none());

        let config = load_config_from_str(STAGE_OVERRIDE_TOML, "prod").unwrap();
        let wsgi = &config.handlers["wsgi"];
        assert!(wsgi.apigateway.is_some());
        assert_eq!(
            wsgi.apigateway.as_ref().unwrap().domain,
            Some("api.example.com".to_string())
        );
    }

    #[test]
    fn test_pocket_key_calculation() {
        let config = load_config_from_str(MINIMAL_TOML, "prod").unwrap();
        let secrets = config.secrets.unwrap();
        assert_eq!(secrets.pocket_key, "prod-myapp-pocket");
    }

    #[test]
    fn test_default_namespace_and_prefix() {
        let toml = r#"
[general]
region = "us-east-1"
project_name = "test"
stages = ["dev"]
"#;
        let config = load_config_from_str(toml, "dev").unwrap();
        assert_eq!(config.namespace, "pocket");
        assert_eq!(config.prefix_template, "{stage}-{project}-{namespace}-");
        assert_eq!(config.resource_prefix, "dev-test-pocket-");
    }

    #[test]
    fn test_deep_merge() {
        let mut target: toml::Value = toml::from_str(
            r#"
[a]
x = 1
y = 2
[a.nested]
foo = "bar"
"#,
        )
        .unwrap();

        let source: toml::Value = toml::from_str(
            r#"
[a]
y = 3
z = 4
[a.nested]
baz = "qux"
"#,
        )
        .unwrap();

        deep_merge(&mut target, &source);

        let a = target.get("a").unwrap();
        assert_eq!(a.get("x").unwrap().as_integer(), Some(1));
        assert_eq!(a.get("y").unwrap().as_integer(), Some(3)); // overridden
        assert_eq!(a.get("z").unwrap().as_integer(), Some(4)); // added
        let nested = a.get("nested").unwrap();
        assert_eq!(nested.get("foo").unwrap().as_str(), Some("bar")); // kept
        assert_eq!(nested.get("baz").unwrap().as_str(), Some("qux")); // added
    }
}
