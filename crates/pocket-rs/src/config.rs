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
    /// 明示パス。省略時は `type` から正準パスを導出する。
    #[serde(default)]
    name: Option<String>,
    /// type 基準 (stored mode): 省略した `name` を `/{pocket_key}-user/{type}` へ導出する。
    #[serde(default, rename = "type")]
    secret_type: Option<String>,
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

/// general.project_name を必須として取り出す。
///
/// Python CLI は pyproject.toml から導出できるが、Rust runtime には
/// その情報源が無い。silent に "unknown" へ fallback すると誤った
/// pocket_key を参照して secrets 空 / queue URL 欠落が黙って起きるため、
/// 明示エラーにする。
fn require_project_name(project_name: Option<String>) -> Result<String> {
    project_name.ok_or_else(|| {
        PocketError::Config(
            "general.project_name is required for the Rust runtime \
             (the Python CLI derives it from pyproject.toml, which is not \
             available here). Set it explicitly in pocket.toml."
                .into(),
        )
    })
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
    let data: toml::Value = toml::from_str(&content).map_err(PocketError::TomlParse)?;

    let general: GeneralToml = {
        let general_val = data
            .get("general")
            .ok_or_else(|| PocketError::Config("missing [general] section".into()))?;
        general_val.clone().try_into().map_err(PocketError::TomlParse)?
    };

    let project_name = require_project_name(general.project_name)?;

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
    let mut data: toml::Value = toml::from_str(content).map_err(PocketError::TomlParse)?;

    // [general] 欠如を先に検出する (stages 取得の失敗を StageNotFound と
    // 誤報告しないため)
    if data.get("general").is_none() {
        return Err(PocketError::Config("missing [general] section".into()));
    }

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

    let project_name = require_project_name(general.project_name)?;

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

        let secrets_config = match ac.secrets {
            Some(sc) => {
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
                let mut user = HashMap::new();
                for (k, v) in sc.user {
                    let spec_store = v.store.as_deref().map(parse_store_type);
                    // name は type 基準の正準パスへ解決してから保持する
                    // (Python の SecretsContext.from_settings と同じ resolve)。
                    let name = match v.name {
                        Some(n) => format_vars(&n),
                        None => match v.secret_type {
                            Some(t) => {
                                let effective_store = spec_store.clone().unwrap_or(store.clone());
                                user_secret_path(&pocket_key, &t, &effective_store)
                            }
                            None => {
                                return Err(PocketError::Config(format!(
                                    "user secret `{k}` must have either `name` or `type`"
                                )));
                            }
                        },
                    };
                    user.insert(
                        k,
                        UserSecretSpec {
                            name,
                            store: spec_store,
                        },
                    );
                }

                Some(SecretsConfig {
                    store,
                    pocket_key,
                    stage: stage.to_string(),
                    project_name: project_name.clone(),
                    region: general.region.clone(),
                    managed,
                    user,
                })
            }
            None => None,
        };

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

/// stored user secret の正準名を type 基準で導出する。
///
/// Python 側 `pocket.context.user_secret_path` と一致させる。provisioning identity を
/// 安定させるため `segment` には backend の type (`neon_database_url` 等) を渡す
/// (consumer の env var 名 = 辞書キーには依存させない)。managed の
/// `/{pocket_key}/...` と衝突させないため `{pocket_key}-user` prefix 配下に置く。
fn user_secret_path(pocket_key: &str, segment: &str, store: &StoreType) -> String {
    let prefix = format!("{pocket_key}-user");
    match store {
        StoreType::Ssm => format!("/{prefix}/{segment}"),
        StoreType::Sm => format!("{prefix}/{segment}"),
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

    // 標準構成: provisioning = "command" + type 基準 user secret (name 省略)。
    // 0.12 の type 基準 canonical 導出 (/{pocket_key}-user/{type}) を Rust でも
    // 解決できることの回帰テスト。
    const TYPE_USER_SECRET_TOML: &str = r#"
[general]
region = "ap-northeast-1"
project_name = "myapp"
stages = ["sandbox"]

[neon]
provisioning = "command"

[awscontainer]
dockerfile_path = "Dockerfile"

[awscontainer.secrets]
store = "ssm"

[awscontainer.secrets.user]
DATABASE_URL = { type = "neon_database_url" }
"#;

    #[test]
    fn test_type_based_user_secret_derives_canonical_path() {
        let config = load_config_from_str(TYPE_USER_SECRET_TOML, "sandbox").unwrap();
        let secrets = config.secrets.unwrap();
        assert_eq!(secrets.pocket_key, "sandbox-myapp-pocket");
        let db = &secrets.user["DATABASE_URL"];
        // store = ssm なので先頭スラッシュ付きの正準パス
        assert_eq!(db.name, "/sandbox-myapp-pocket-user/neon_database_url");
        // spec 個別 store は未指定 (secrets.store を継承)
        assert_eq!(db.store, None);
    }

    #[test]
    fn test_type_based_user_secret_sm_store_has_no_leading_slash() {
        // store 省略 (default = sm) の type 基準 user secret は先頭スラッシュ無し。
        let toml = r#"
[general]
region = "ap-northeast-1"
project_name = "myapp"
stages = ["dev"]

[awscontainer]
dockerfile_path = "Dockerfile"

[awscontainer.secrets.user]
DATABASE_URL = { type = "neon_database_url" }
"#;
        let config = load_config_from_str(toml, "dev").unwrap();
        let db = &config.secrets.unwrap().user["DATABASE_URL"];
        assert_eq!(db.name, "dev-myapp-pocket-user/neon_database_url");
    }

    #[test]
    fn test_user_secret_per_spec_store_overrides_derivation() {
        // spec の store が secrets.store を上書きし、導出パスの形式も従う。
        let toml = r#"
[general]
region = "ap-northeast-1"
project_name = "myapp"
stages = ["dev"]

[awscontainer]
dockerfile_path = "Dockerfile"

[awscontainer.secrets]
store = "sm"

[awscontainer.secrets.user]
DATABASE_URL = { type = "neon_database_url", store = "ssm" }
"#;
        let config = load_config_from_str(toml, "dev").unwrap();
        let db = &config.secrets.unwrap().user["DATABASE_URL"];
        assert_eq!(db.store, Some(StoreType::Ssm));
        assert_eq!(db.name, "/dev-myapp-pocket-user/neon_database_url");
    }

    #[test]
    fn test_name_based_user_secret_still_works() {
        let config = load_config_from_str(MINIMAL_TOML, "dev").unwrap();
        let ext = &config.secrets.unwrap().user["EXTERNAL_API_KEY"];
        assert_eq!(ext.name, "my-external-key");
        assert_eq!(ext.store, Some(StoreType::Sm));
    }

    #[test]
    fn test_user_secret_without_name_or_type_errors() {
        let toml = r#"
[general]
region = "ap-northeast-1"
project_name = "myapp"
stages = ["dev"]

[awscontainer]
dockerfile_path = "Dockerfile"

[awscontainer.secrets.user]
DATABASE_URL = { store = "ssm" }
"#;
        let err = load_config_from_str(toml, "dev").unwrap_err();
        assert!(matches!(err, PocketError::Config(_)));
    }

    #[test]
    fn test_missing_project_name_errors() {
        // Python は pyproject.toml から導出するが Rust には情報源が無い。
        // silent な "unknown" fallback は誤った pocket_key の参照になる
        let toml = r#"
[general]
region = "ap-northeast-1"
stages = ["dev"]
"#;
        let err = load_config_from_str(toml, "dev").unwrap_err();
        assert!(err.to_string().contains("project_name"));
    }

    #[test]
    fn test_missing_general_section_reports_config_error() {
        // 以前は stages 取得失敗が StageNotFound と誤報告されていた
        let err = load_config_from_str("[dev]\n", "dev").unwrap_err();
        assert!(err.to_string().contains("missing [general] section"));
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
