use std::collections::{BTreeSet, HashMap};
use std::path::{Path, PathBuf};

use serde::Deserialize;

use crate::error::{PocketError, Result};

/// enable_origin_verify 用 managed secret の予約キー (= Lambda runtime env 名)。
/// Python 側 pocket.context.ORIGIN_VERIFY_SECRET_KEY と一致させること。
pub const ORIGIN_VERIFY_SECRET_KEY: &str = "POCKET_ORIGIN_VERIFY_SECRET";

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
    /// 自 container 名 (`[container.<name>]` の name)。POCKET_CONTAINER env
    /// (CFn が注入) か、container が 1 つだけならそれ。
    pub container_name: String,
    /// container store ({stage}-{project}-{name}-{namespace}) の view
    /// (shared = true でない managed 宣言)
    pub secrets: Option<SecretsConfig>,
    /// shared store ({stage}-{project}-{namespace}) の view
    /// (shared = true の managed + user secret)
    pub shared_secrets: Option<SecretsConfig>,
    /// 自 container の handlers
    pub handlers: HashMap<String, HandlerConfig>,
    /// 全 container の handlers (container 名 → handlers)。
    /// 他 container の queue / host を POCKET_<CONTAINER>_<HANDLER>_* に注入する
    pub containers: HashMap<String, HashMap<String, HandlerConfig>>,
    /// `[cloudfront.<name>]` の name 一覧 (sorted)。
    /// distribution ドメインを POCKET_CLOUDFRONT_<NAME>_DOMAIN に注入するのに使う。
    pub cloudfront_names: Vec<String>,
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
struct ContainerToml {
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
    /// true なら project 共有 store に保存 (同名宣言の値共有)。既定は container store
    #[serde(default)]
    shared: bool,
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

/// `[cloudfront.<name>]` のうち runtime が必要とするキーのみ読む
/// (routes 等は CLI 専用なので無視する)
#[derive(Debug, Deserialize)]
struct CloudFrontToml {
    #[serde(default)]
    enable_origin_verify: bool,
    waf: Option<WafToml>,
}

#[derive(Debug, Deserialize)]
struct WafToml {
    #[serde(default)]
    allow_rules: Vec<WafAllowRuleToml>,
}

#[derive(Debug, Deserialize)]
struct WafAllowRuleToml {
    /// managed secret のキー名 (path のみのルールでは None)
    #[serde(default)]
    header: Option<String>,
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
///
/// 自 container は POCKET_CONTAINER env (CFn が各 function に注入) で選択する。
/// env が無い場合は container が 1 つだけならそれを使う。
pub fn load_config(stage: &str) -> Result<PocketConfig> {
    let toml_path = find_toml_path()?;
    load_config_from_path(&toml_path, stage)
}

fn env_container_name() -> Option<String> {
    std::env::var("POCKET_CONTAINER")
        .ok()
        .filter(|s| !s.is_empty())
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
        general_val
            .clone()
            .try_into()
            .map_err(PocketError::TomlParse)?
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
        container_name: String::new(),
        secrets: None,
        shared_secrets: None,
        handlers: HashMap::new(),
        containers: HashMap::new(),
        cloudfront_names: Vec::new(),
    })
}

/// 指定パスの pocket.toml をパースして PocketConfig を返す
pub fn load_config_from_path(path: &Path, stage: &str) -> Result<PocketConfig> {
    let content = std::fs::read_to_string(path).map_err(PocketError::Io)?;
    load_config_from_str(&content, stage, env_container_name().as_deref())
}

/// TOML 文字列から PocketConfig を構築する
///
/// `container` は自 container 名。None の場合、container が 1 つだけならそれを
/// 選択し、複数なら Config エラー (silent に誤った container を選ばない)。
pub fn load_config_from_str(
    content: &str,
    stage: &str,
    container: Option<&str>,
) -> Result<PocketConfig> {
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
        general_val
            .clone()
            .try_into()
            .map_err(PocketError::TomlParse)?
    };

    let project_name = require_project_name(general.project_name)?;

    let format_vars = |s: &str| -> String {
        s.replace("{stage}", stage)
            .replace("{project}", &project_name)
            .replace("{namespace}", &general.namespace)
    };

    let resource_prefix = format_vars(&general.prefix_template);
    let slug = format!("{}-{}", stage, project_name);

    // 旧 [awscontainer] (0.29.0 で廃止) は移行案内付きで明示エラー
    if data.get("awscontainer").is_some() {
        return Err(PocketError::Config(
            "[awscontainer] は廃止されました。[container.<name>] の dict 形式に \
             移行してください (CHANGELOG 0.29.0 参照)。"
                .into(),
        ));
    }

    // [container.<name>] セクション群をパースする
    let container_tables: Vec<(String, ContainerToml)> = match data.get("container") {
        Some(v) => {
            let table = v.as_table().ok_or_else(|| {
                PocketError::Config("[container] must be a table of containers".into())
            })?;
            let mut parsed = Vec::new();
            for (name, c_val) in table {
                let c: ContainerToml = c_val.clone().try_into().map_err(PocketError::TomlParse)?;
                parsed.push((name.clone(), c));
            }
            parsed
        }
        None => Vec::new(),
    };

    // 自 container の選択 (POCKET_CONTAINER env > 単一 container)
    let container_name = match container {
        Some(name) => {
            if !container_tables.iter().any(|(n, _)| n == name) {
                return Err(PocketError::Config(format!(
                    "container `{name}` は設定にありません"
                )));
            }
            name.to_string()
        }
        None => match container_tables.len() {
            0 => String::new(),
            1 => container_tables[0].0.clone(),
            _ => {
                return Err(PocketError::Config(
                    "複数の container が定義されています。POCKET_CONTAINER \
                     環境変数で自 container を指定してください。"
                        .into(),
                ));
            }
        },
    };

    let build_handlers =
        |c_name: &str, handlers: HashMap<String, HandlerToml>| -> HashMap<String, HandlerConfig> {
            handlers
                .into_iter()
                .map(|(key, h)| {
                    let apigateway = h
                        .apigateway
                        .map(|ag| ApiGatewayConfig { domain: ag.domain });
                    let sqs = if h.sqs.is_some() {
                        // Python 側 SqsContext.from_settings と同じ導出:
                        // {prefix}{container}-{handler}
                        let queue_name = format!("{resource_prefix}{c_name}-{key}");
                        Some(SqsConfig { name: queue_name })
                    } else {
                        None
                    };
                    (key, HandlerConfig { apigateway, sqs })
                })
                .collect()
        };

    let mut containers_config: HashMap<String, HashMap<String, HandlerConfig>> = HashMap::new();
    let mut own_secrets_toml: Option<SecretsToml> = None;
    // secrets を宣言するいずれかの container の (store, pocket_key_format)。
    // Python の project_secrets_base 相当 (全 container での一致は settings 側で
    // 検証済みなので最初に見つかった宣言でよい)
    let mut declared_store_format: Option<(String, String)> = None;
    for (name, c) in container_tables {
        if let Some(sc) = &c.secrets {
            declared_store_format
                .get_or_insert_with(|| (sc.store.clone(), sc.pocket_key_format.clone()));
        }
        if name == container_name {
            own_secrets_toml = c.secrets;
        }
        containers_config.insert(name.clone(), build_handlers(&name, c.handlers));
    }
    let handlers_config = containers_config
        .get(&container_name)
        .cloned()
        .unwrap_or_default();

    // [cloudfront.<name>] をパースする。name は distribution ドメインの env 注入用
    // (順序は決定的にするため sorted)。enable_origin_verify / waf.allow_rules は
    // 自動注入 managed secret の導出に使う (値は CFn stack output / secret store
    // から引くのでここでは宣言だけ読む)
    let mut cloudfront_names: Vec<String> = Vec::new();
    let mut cloudfront_tables: Vec<CloudFrontToml> = Vec::new();
    if let Some(table) = data.get("cloudfront").and_then(|v| v.as_table()) {
        for (name, c_val) in table {
            cloudfront_names.push(name.clone());
            let c: CloudFrontToml = c_val.clone().try_into().map_err(PocketError::TomlParse)?;
            cloudfront_tables.push(c);
        }
    }
    cloudfront_names.sort();

    // 自動注入 managed secret (Python の pocket.context._injected_managed_specs と
    // 同じ導出)。enable_origin_verify の検証用 secret と waf.allow_rules の header
    // secret は shared store の managed 扱いで runtime env に載せる
    let mut injected: Vec<(String, ManagedSecretSpec)> = Vec::new();
    if cloudfront_tables.iter().any(|c| c.enable_origin_verify) {
        injected.push((
            ORIGIN_VERIFY_SECRET_KEY.to_string(),
            ManagedSecretSpec {
                secret_type: "origin_verify_secret".to_string(),
                options: HashMap::new(),
            },
        ));
    }
    let waf_headers: BTreeSet<String> = cloudfront_tables
        .iter()
        .filter_map(|c| c.waf.as_ref())
        .flat_map(|w| w.allow_rules.iter())
        .filter_map(|r| r.header.clone())
        .collect();
    for header in waf_headers {
        injected.push((
            header,
            ManagedSecretSpec {
                secret_type: "waf_allow_secret".to_string(),
                options: HashMap::new(),
            },
        ));
    }

    // shared store (project 共有) と container store の 2 view を組み立てる。
    // shared = true の managed + user secret + 自動注入 secret は project パス
    // ({stage}-{project}-{namespace})、無印の managed は container パス
    // ({stage}-{project}-{name}-{namespace}) に保存される (Python 側と同じ導出)。
    // store / pocket_key_format は自 container の宣言 > 宣言のある container からの
    // 継承 > 既定値 (Python の project_secrets_base 相当)。自 container に secrets
    // 宣言が無くても、自動注入があれば shared view を作る。
    let (base_store, base_format) = match &own_secrets_toml {
        Some(sc) => (sc.store.clone(), sc.pocket_key_format.clone()),
        None => {
            declared_store_format.unwrap_or_else(|| (default_store(), default_pocket_key_format()))
        }
    };
    let store = parse_store_type(&base_store);
    let project_key = format_vars(&base_format);
    let container_key = base_format
        .replace("{stage}", stage)
        .replace("{project}", &project_name)
        .replace(
            "{namespace}",
            &format!("{container_name}-{}", general.namespace),
        );

    let mut unshared_managed = HashMap::new();
    let mut shared_managed = HashMap::new();
    let mut user = HashMap::new();
    if let Some(sc) = own_secrets_toml {
        for (k, v) in sc.managed {
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
            let spec = ManagedSecretSpec {
                secret_type: v.secret_type,
                options,
            };
            if v.shared {
                shared_managed.insert(k, spec);
            } else {
                unshared_managed.insert(k, spec);
            }
        }
        for (k, v) in sc.user {
            let spec_store = v.store.as_deref().map(parse_store_type);
            // name は type 基準の正準パスへ解決してから保持する
            // (Python の SecretsContext.from_settings と同じ resolve)。
            // 正準パスは project 側の pocket_key から導出する。
            // 同時指定は Python (check_user_name_type_exclusive) と同じく排他エラー。
            let name = match (v.name, v.secret_type) {
                (Some(_), Some(_)) => {
                    return Err(PocketError::Config(format!(
                        "user secret `{k}`: `name` and `type` are mutually \
                         exclusive (name = 明示参照 / type = stored mode)"
                    )));
                }
                (Some(n), None) => format_vars(&n),
                (None, Some(t)) => {
                    let effective_store = spec_store.clone().unwrap_or(store.clone());
                    user_secret_path(&project_key, &t, &effective_store)
                }
                (None, None) => {
                    return Err(PocketError::Config(format!(
                        "user secret `{k}` must have either `name` or `type`"
                    )));
                }
            };
            user.insert(
                k,
                UserSecretSpec {
                    name,
                    store: spec_store,
                },
            );
        }
    }
    // 注入 spec は宣言より優先する (Python の `{**shared_managed, **injected}` と
    // 同じ。キー衝突は settings 側で検証済み)
    for (k, spec) in injected {
        shared_managed.insert(k, spec);
    }

    let make_config = |pocket_key: String,
                       managed: HashMap<String, ManagedSecretSpec>,
                       user: HashMap<String, UserSecretSpec>|
     -> Option<SecretsConfig> {
        if managed.is_empty() && user.is_empty() {
            return None;
        }
        Some(SecretsConfig {
            store: store.clone(),
            pocket_key,
            stage: stage.to_string(),
            project_name: project_name.clone(),
            region: general.region.clone(),
            managed,
            user,
        })
    };
    let secrets_config = make_config(container_key, unshared_managed, HashMap::new());
    let shared_secrets_config = make_config(project_key, shared_managed, user);

    Ok(PocketConfig {
        region: general.region,
        project_name,
        namespace: general.namespace,
        prefix_template: general.prefix_template,
        stage: stage.to_string(),
        slug,
        resource_prefix,
        container_name,
        secrets: secrets_config,
        shared_secrets: shared_secrets_config,
        handlers: handlers_config,
        containers: containers_config,
        cloudfront_names,
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

[container.main]
dockerfile_path = "Dockerfile"

[container.main.secrets]
store = "ssm"
pocket_key_format = "{stage}-{project}-{namespace}"

[container.main.secrets.managed]
SECRET_KEY = { type = "password", options = { length = "50" } }
DATABASE_URL = { type = "neon_database_url" }

[container.main.secrets.user]
EXTERNAL_API_KEY = { name = "my-external-key", store = "sm" }

[container.main.handlers.wsgi]
command = "handler.wsgi"

[container.main.handlers.wsgi.apigateway]
domain = "api.example.com"

[container.main.handlers.worker]
command = "handler.worker"
timeout = 600
sqs = {}
"#;

    const STAGE_OVERRIDE_TOML: &str = r#"
[general]
region = "ap-northeast-1"
project_name = "myapp"
stages = ["dev", "prod"]

[container.main]
dockerfile_path = "Dockerfile"

[container.main.handlers.wsgi]
command = "handler.wsgi"

[dev.container.main.handlers.wsgi]
apigateway = {}

[prod.container.main.handlers.wsgi.apigateway]
domain = "api.example.com"
"#;

    const MULTI_CONTAINER_TOML: &str = r#"
[general]
region = "ap-northeast-1"
project_name = "myapp"
stages = ["dev"]

[container.mydjango]
dockerfile_path = "Dockerfile"

[container.mydjango.handlers.wsgi]
command = "pocket.django.lambda_handlers.wsgi_handler"
apigateway = {}

[container.v2]
dockerfile_path = "v2/Dockerfile"

[container.v2.handlers.wsgi]
command = "admin-v2"
apigateway = {}

[container.v2.handlers.worker]
command = "admin-v2"
sqs = {}
"#;

    #[test]
    fn test_load_basic_config() {
        let config = load_config_from_str(MINIMAL_TOML, "dev", None).unwrap();
        assert_eq!(config.region, "ap-northeast-1");
        assert_eq!(config.project_name, "myapp");
        assert_eq!(config.namespace, "pocket");
        assert_eq!(config.stage, "dev");
        assert_eq!(config.slug, "dev-myapp");
        assert_eq!(config.resource_prefix, "dev-myapp-pocket-");
        // container が 1 つだけなら省略時にそれが選択される
        assert_eq!(config.container_name, "main");
    }

    #[test]
    fn test_secrets_config() {
        let config = load_config_from_str(MINIMAL_TOML, "dev", None).unwrap();
        // shared でない managed は container store (pocket_key に container 名入り)
        let secrets = config.secrets.unwrap();
        assert_eq!(secrets.store, StoreType::Ssm);
        assert_eq!(secrets.pocket_key, "dev-myapp-main-pocket");
        assert_eq!(secrets.managed.len(), 2);
        assert!(secrets.managed.contains_key("SECRET_KEY"));
        assert!(secrets.managed.contains_key("DATABASE_URL"));
        assert!(secrets.user.is_empty());
        // user secret は shared store (project パス) の view に載る
        let shared = config.shared_secrets.unwrap();
        assert_eq!(shared.pocket_key, "dev-myapp-pocket");
        assert_eq!(shared.user.len(), 1);
        let ext = &shared.user["EXTERNAL_API_KEY"];
        assert_eq!(ext.name, "my-external-key");
        assert_eq!(ext.store, Some(StoreType::Sm));
    }

    #[test]
    fn test_shared_managed_secret_goes_to_project_store() {
        let toml = r#"
[general]
region = "ap-northeast-1"
project_name = "myapp"
stages = ["dev"]

[container.main]
dockerfile_path = "Dockerfile"

[container.main.secrets.managed]
SECRET_KEY = { type = "password", shared = true }
LOCAL_TOKEN = { type = "password" }
"#;
        let config = load_config_from_str(toml, "dev", None).unwrap();
        let container_store = config.secrets.unwrap();
        assert_eq!(container_store.pocket_key, "dev-myapp-main-pocket");
        assert!(container_store.managed.contains_key("LOCAL_TOKEN"));
        assert!(!container_store.managed.contains_key("SECRET_KEY"));
        let shared = config.shared_secrets.unwrap();
        assert_eq!(shared.pocket_key, "dev-myapp-pocket");
        assert!(shared.managed.contains_key("SECRET_KEY"));
    }

    #[test]
    fn test_handlers_config() {
        let config = load_config_from_str(MINIMAL_TOML, "dev", None).unwrap();
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
        // queue 名は {prefix}{container}-{handler}
        assert_eq!(
            worker.sqs.as_ref().unwrap().name,
            "dev-myapp-pocket-main-worker"
        );
    }

    #[test]
    fn test_legacy_awscontainer_rejected() {
        let toml = r#"
[general]
region = "ap-northeast-1"
project_name = "myapp"
stages = ["dev"]

[awscontainer]
dockerfile_path = "Dockerfile"
"#;
        let err = load_config_from_str(toml, "dev", None).unwrap_err();
        assert!(err.to_string().contains("[awscontainer]"), "got {err}");
        assert!(err.to_string().contains("[container.<name>]"), "got {err}");
    }

    #[test]
    fn test_multi_container_requires_selection() {
        let err = load_config_from_str(MULTI_CONTAINER_TOML, "dev", None).unwrap_err();
        assert!(err.to_string().contains("POCKET_CONTAINER"), "got {err}");
    }

    #[test]
    fn test_multi_container_selects_by_name() {
        let config = load_config_from_str(MULTI_CONTAINER_TOML, "dev", Some("v2")).unwrap();
        assert_eq!(config.container_name, "v2");
        assert_eq!(config.handlers.len(), 2);
        assert_eq!(
            config.handlers["worker"].sqs.as_ref().unwrap().name,
            "dev-myapp-pocket-v2-worker"
        );
        // 他 container の handlers も containers から引ける
        assert!(config.containers["mydjango"].contains_key("wsgi"));
    }

    #[test]
    fn test_unknown_container_selection_errors() {
        let err = load_config_from_str(MULTI_CONTAINER_TOML, "dev", Some("nope")).unwrap_err();
        assert!(err.to_string().contains("nope"), "got {err}");
    }

    #[test]
    fn test_cloudfront_names_collected_sorted() {
        let toml = r#"
[general]
region = "ap-northeast-1"
project_name = "myapp"
stages = ["dev"]

[s3]

[cloudfront.web]
domain = "example.com"

[cloudfront.admin]
"#;
        let config = load_config_from_str(toml, "dev", None).unwrap();
        // env 注入を決定的にするため sorted
        assert_eq!(config.cloudfront_names, vec!["admin", "web"]);
        // stack 名の導出が Python の CloudFrontContext.slug ({stage}-{project}-{name})
        // と一致すること
        assert_eq!(config.slug, "dev-myapp");
    }

    #[test]
    fn test_cloudfront_names_empty_when_absent() {
        let config = load_config_from_str(MINIMAL_TOML, "dev", None).unwrap();
        assert!(config.cloudfront_names.is_empty());
    }

    #[test]
    fn test_origin_verify_secret_injected_without_secrets_section() {
        // Python の _injected_managed_specs 相当: secrets 宣言が無い container でも
        // enable_origin_verify があれば shared view が作られ env に載る
        let toml = r#"
[general]
region = "ap-northeast-1"
project_name = "myapp"
stages = ["dev"]

[container.main]
dockerfile_path = "Dockerfile"

[container.main.handlers.wsgi]
command = "app"
apigateway = {}

[cloudfront.web]
enable_origin_verify = true
"#;
        let config = load_config_from_str(toml, "dev", None).unwrap();
        // container store 側 view は作られない
        assert!(config.secrets.is_none());
        let shared = config.shared_secrets.unwrap();
        // 既定 store / 既定 pocket_key_format (project パス)
        assert_eq!(shared.store, StoreType::Sm);
        assert_eq!(shared.pocket_key, "dev-myapp-pocket");
        assert_eq!(shared.managed.len(), 1);
        let spec = &shared.managed[ORIGIN_VERIFY_SECRET_KEY];
        assert_eq!(spec.secret_type, "origin_verify_secret");
    }

    #[test]
    fn test_origin_verify_disabled_or_absent_injects_nothing() {
        let toml = r#"
[general]
region = "ap-northeast-1"
project_name = "myapp"
stages = ["dev"]

[container.main]
dockerfile_path = "Dockerfile"

[cloudfront.web]
enable_origin_verify = false

[cloudfront.admin]
"#;
        let config = load_config_from_str(toml, "dev", None).unwrap();
        assert!(config.secrets.is_none());
        assert!(config.shared_secrets.is_none());
    }

    #[test]
    fn test_waf_allow_headers_injected_sorted_dedup() {
        // header 付き allow_rules は宣言キー名の managed secret になる。
        // path のみのルールは対象外、複数 distribution 越しの同名は dedup
        let toml = r#"
[general]
region = "ap-northeast-1"
project_name = "myapp"
stages = ["dev"]

[container.main]
dockerfile_path = "Dockerfile"

[cloudfront.web.waf]
allow_rules = [
    { header = "ZULU_TOKEN" },
    { path = "/healthz" },
    { path = "/smoke", header = "ALPHA_TOKEN" },
]

[cloudfront.admin]
enable_origin_verify = true

[cloudfront.admin.waf]
allow_rules = [{ header = "ZULU_TOKEN" }]
"#;
        let config = load_config_from_str(toml, "dev", None).unwrap();
        let shared = config.shared_secrets.unwrap();
        assert_eq!(shared.managed.len(), 3);
        assert_eq!(
            shared.managed["ALPHA_TOKEN"].secret_type,
            "waf_allow_secret"
        );
        assert_eq!(shared.managed["ZULU_TOKEN"].secret_type, "waf_allow_secret");
        assert_eq!(
            shared.managed[ORIGIN_VERIFY_SECRET_KEY].secret_type,
            "origin_verify_secret"
        );
    }

    #[test]
    fn test_injected_inherits_store_format_from_other_container() {
        // 自 container (v2) に secrets 宣言が無くても、宣言のある container
        // (mydjango) から store / pocket_key_format を継承する
        // (Python の project_secrets_base 相当)
        let toml = r#"
[general]
region = "ap-northeast-1"
project_name = "myapp"
stages = ["dev"]

[container.mydjango]
dockerfile_path = "Dockerfile"

[container.mydjango.secrets]
store = "ssm"

[container.mydjango.secrets.managed]
SECRET_KEY = { type = "password" }

[container.v2]
dockerfile_path = "v2/Dockerfile"

[cloudfront.web]
enable_origin_verify = true
"#;
        let config = load_config_from_str(toml, "dev", Some("v2")).unwrap();
        assert!(config.secrets.is_none());
        let shared = config.shared_secrets.unwrap();
        assert_eq!(shared.store, StoreType::Ssm);
        assert_eq!(shared.pocket_key, "dev-myapp-pocket");
        assert!(shared.managed.contains_key(ORIGIN_VERIFY_SECRET_KEY));
        // mydjango の無印 managed は v2 の view には載らない
        assert!(!shared.managed.contains_key("SECRET_KEY"));
    }

    #[test]
    fn test_injected_merges_with_declared_shared_secrets() {
        let toml = r#"
[general]
region = "ap-northeast-1"
project_name = "myapp"
stages = ["dev"]

[container.main]
dockerfile_path = "Dockerfile"

[container.main.secrets.managed]
SHARED_KEY = { type = "password", shared = true }
LOCAL_KEY = { type = "password" }

[cloudfront.web]
enable_origin_verify = true
"#;
        let config = load_config_from_str(toml, "dev", None).unwrap();
        let container_store = config.secrets.unwrap();
        assert!(container_store.managed.contains_key("LOCAL_KEY"));
        assert!(!container_store
            .managed
            .contains_key(ORIGIN_VERIFY_SECRET_KEY));
        let shared = config.shared_secrets.unwrap();
        assert_eq!(shared.managed.len(), 2);
        assert!(shared.managed.contains_key("SHARED_KEY"));
        assert!(shared.managed.contains_key(ORIGIN_VERIFY_SECRET_KEY));
    }

    #[test]
    fn test_stage_not_found() {
        let err = load_config_from_str(MINIMAL_TOML, "staging", None).unwrap_err();
        assert!(matches!(err, PocketError::StageNotFound(_)));
    }

    #[test]
    fn test_stage_merge() {
        let config = load_config_from_str(STAGE_OVERRIDE_TOML, "dev", None).unwrap();
        let wsgi = &config.handlers["wsgi"];
        // dev ステージは apigateway = {} なので domain なし
        assert!(wsgi.apigateway.is_some());
        assert!(wsgi.apigateway.as_ref().unwrap().domain.is_none());

        let config = load_config_from_str(STAGE_OVERRIDE_TOML, "prod", None).unwrap();
        let wsgi = &config.handlers["wsgi"];
        assert!(wsgi.apigateway.is_some());
        assert_eq!(
            wsgi.apigateway.as_ref().unwrap().domain,
            Some("api.example.com".to_string())
        );
    }

    #[test]
    fn test_pocket_key_calculation() {
        let config = load_config_from_str(MINIMAL_TOML, "prod", None).unwrap();
        assert_eq!(config.secrets.unwrap().pocket_key, "prod-myapp-main-pocket");
        assert_eq!(
            config.shared_secrets.unwrap().pocket_key,
            "prod-myapp-pocket"
        );
    }

    #[test]
    fn test_default_namespace_and_prefix() {
        let toml = r#"
[general]
region = "us-east-1"
project_name = "test"
stages = ["dev"]
"#;
        let config = load_config_from_str(toml, "dev", None).unwrap();
        assert_eq!(config.namespace, "pocket");
        assert_eq!(config.prefix_template, "{stage}-{project}-{namespace}-");
        assert_eq!(config.resource_prefix, "dev-test-pocket-");
        assert_eq!(config.container_name, "");
        assert!(config.containers.is_empty());
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

[container.main]
dockerfile_path = "Dockerfile"

[container.main.secrets]
store = "ssm"

[container.main.secrets.user]
DATABASE_URL = { type = "neon_database_url" }
"#;

    #[test]
    fn test_type_based_user_secret_derives_canonical_path() {
        let config = load_config_from_str(TYPE_USER_SECRET_TOML, "sandbox", None).unwrap();
        // user secret は shared store view。正準パスは project 側の pocket_key
        let secrets = config.shared_secrets.unwrap();
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

[container.main]
dockerfile_path = "Dockerfile"

[container.main.secrets.user]
DATABASE_URL = { type = "neon_database_url" }
"#;
        let config = load_config_from_str(toml, "dev", None).unwrap();
        let db = &config.shared_secrets.unwrap().user["DATABASE_URL"];
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

[container.main]
dockerfile_path = "Dockerfile"

[container.main.secrets]
store = "sm"

[container.main.secrets.user]
DATABASE_URL = { type = "neon_database_url", store = "ssm" }
"#;
        let config = load_config_from_str(toml, "dev", None).unwrap();
        let db = &config.shared_secrets.unwrap().user["DATABASE_URL"];
        assert_eq!(db.store, Some(StoreType::Ssm));
        assert_eq!(db.name, "/dev-myapp-pocket-user/neon_database_url");
    }

    #[test]
    fn test_name_based_user_secret_still_works() {
        let config = load_config_from_str(MINIMAL_TOML, "dev", None).unwrap();
        let ext = &config.shared_secrets.unwrap().user["EXTERNAL_API_KEY"];
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

[container.main]
dockerfile_path = "Dockerfile"

[container.main.secrets.user]
DATABASE_URL = { store = "ssm" }
"#;
        let err = load_config_from_str(toml, "dev", None).unwrap_err();
        assert!(matches!(err, PocketError::Config(_)));
    }

    #[test]
    fn test_user_secret_with_both_name_and_type_errors() {
        // Python の check_user_name_type_exclusive と同じ排他。以前は name 優先で
        // type を黙殺しており、stored mode のつもりの設定が明示参照になっていた
        let toml = r#"
[general]
region = "ap-northeast-1"
project_name = "myapp"
stages = ["dev"]

[container.main]
dockerfile_path = "Dockerfile"

[container.main.secrets.user]
DATABASE_URL = { name = "my-secret", type = "neon_database_url" }
"#;
        let err = load_config_from_str(toml, "dev", None).unwrap_err();
        assert!(err.to_string().contains("mutually"), "got {err}");
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
        let err = load_config_from_str(toml, "dev", None).unwrap_err();
        assert!(err.to_string().contains("project_name"));
    }

    #[test]
    fn test_missing_general_section_reports_config_error() {
        // 以前は stages 取得失敗が StageNotFound と誤報告されていた
        let err = load_config_from_str("[dev]\n", "dev", None).unwrap_err();
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
