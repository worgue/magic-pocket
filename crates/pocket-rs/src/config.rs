use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};

use magic_pocket_settings::model as settings_model;
use magic_pocket_settings::{LoadContext, Settings};

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
    /// `[s3]` の bucket 名 (`bucket_name_format` を展開したもの)。`[s3]` が無ければ None
    pub s3_bucket_name: Option<String>,
    /// `versioning = "object_hash"` の route (cloudfront 名順)。`static_url` の解決に使う
    pub static_routes: Vec<StaticRouteConfig>,
}

/// `versioning = "object_hash"` の route 1 つ分 (Python の RouteContext の導出と同じ)
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StaticRouteConfig {
    /// `[cloudfront.<name>]` の name
    pub cloudfront: String,
    /// `[cloudfront.<name>].domain` (alias)。配信 URL のドメインはこれを優先し、無ければ
    /// runtime が注入する `POCKET_CLOUDFRONT_<NAME>_DOMAIN` (distribution の既定ドメイン)
    pub cloudfront_domain: Option<String>,
    /// route の `ref` (無ければ None)
    pub route_ref: Option<String>,
    /// route の論理名 (path_pattern から導出。`/static/*` → `static`)
    pub route_name: String,
    /// 配信 URL の path prefix (末尾 / 付き。`/static/*` → `/static/`)
    pub url_prefix: String,
    /// release 表と current を置く S3 prefix (`.pocket/static/<cloudfront>/<route_name>/`)
    pub meta_prefix: String,
    /// S3 の key prefix (末尾 / 付き。空なら bucket 直下)。Python の `RouteContext.key_prefix`
    /// と同じ導出 (`origin_path` + path_pattern の prefix)
    pub key_prefix: String,
    /// map ファイルの pocket.toml からの相対パス (`map` 省略時は
    /// `pocket_static/<cloudfront>-<route_name>.map`)
    pub map_path: String,
    /// 配信の `Cache-Control: max-age` (秒)。route の `versioned_max_age`
    pub versioned_max_age: u64,
    /// route の `sources` (publish の入力。runtime は使わない)
    pub sources: Vec<StaticSourceConfig>,
}

/// `versioning = "object_hash"` の route に載せるローカルファイルの集合 (Python の
/// `settings.StaticSource`)。name / dir / prefix の形の検査は publish 側 (CLI) で行う
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StaticSourceConfig {
    pub name: String,
    pub dir: String,
    pub prefix: String,
    pub include: Vec<String>,
    pub exclude: Vec<String>,
    pub hashed: bool,
}

/// pocket.toml のうち `pocket-rs static` (publish 側) が使う部分。runtime の
/// [`PocketConfig`] と違い container を選ばない (複数 container でも読める)
#[derive(Debug, Clone)]
pub struct StaticConfig {
    pub region: String,
    pub project_name: String,
    pub stage: String,
    /// `[cloudfront.<name>]` の name 一覧 (sorted)
    pub cloudfront_names: Vec<String>,
    /// `[s3]` の bucket 名 (`bucket_name_format` を展開したもの)。`[s3]` が無ければ None
    pub s3_bucket_name: Option<String>,
    /// `versioning = "object_hash"` の route (cloudfront 名順、route は定義順)
    pub static_routes: Vec<StaticRouteConfig>,
}

/// Python の `pocket.utils.route_logical_name` と同じ導出。
/// path を "/" で分け、"*" と空を除き、各要素を英数字だけにして "-" で繋ぐ (空なら "root")
pub fn route_logical_name(path_pattern: &str) -> String {
    settings_model::cloudfront::route_logical_name(path_pattern)
}

/// Python の `RouteContext.url_prefix` と同じ導出 (`/static/*` → `/static/`)
pub fn route_url_prefix(path_pattern: &str) -> String {
    format!("{}/", path_pattern.trim_end_matches(['/', '*']))
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

/// Python の `RouteContext.key_prefix` と同じ導出。origin_path 指定時は OriginPath として
/// 付くので origin_path + path_pattern の prefix、省略時は path_pattern の prefix だけ
pub fn route_key_prefix(origin_path: &str, path_pattern: &str) -> String {
    let prefix = format!("{origin_path}{}", path_pattern.trim_end_matches(['/', '*']));
    let prefix = prefix.trim_start_matches('/');
    if prefix.is_empty() {
        String::new()
    } else {
        format!("{prefix}/")
    }
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
    let general_val = data
        .get("general")
        .ok_or_else(|| PocketError::Config("missing [general] section".into()))?;
    require_project_name(
        general_val
            .get("project_name")
            .and_then(|v| v.as_str())
            .map(str::to_string),
    )?;
    let general: settings_model::GeneralSettings = general_val
        .clone()
        .try_into()
        .map_err(PocketError::TomlParse)?;

    Ok(PocketConfig {
        region: general.region,
        project_name: general.project_name,
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
        s3_bucket_name: None,
        static_routes: Vec::new(),
    })
}

/// 指定パスの pocket.toml をパースして PocketConfig を返す
pub fn load_config_from_path(path: &Path, stage: &str) -> Result<PocketConfig> {
    let content = std::fs::read_to_string(path).map_err(PocketError::Io)?;
    load_config_from_str(&content, stage, env_container_name().as_deref())
}

/// pocket.toml を Python と同じ解釈・同じ検証で読む (`magic-pocket-settings`)。
///
/// `[general]` の欠落・未定義の stage・project_name の欠落は従来どおりの
/// エラーにする。project_name は Rust runtime では pyproject.toml から導出できない
/// ので必須。`[neon]` 等の credential は Python と同じく環境変数から補う。
/// 検証を通った後の助言 (Python では stderr の警告) は tracing の warn で出す
fn load_settings(content: &str, stage: &str) -> Result<Settings> {
    let data: toml::Value = toml::from_str(content).map_err(PocketError::TomlParse)?;
    let Some(general) = data.get("general") else {
        return Err(PocketError::Config("missing [general] section".into()));
    };
    let stages: Vec<&str> = general
        .get("stages")
        .and_then(|s| s.as_array())
        .map(|a| a.iter().filter_map(|v| v.as_str()).collect())
        .unwrap_or_default();
    if !stages.contains(&stage) {
        return Err(PocketError::StageNotFound(stage.to_string()));
    }
    let ctx = LoadContext {
        project_name_fallback: String::new(),
        env: std::env::vars().collect::<BTreeMap<_, _>>(),
        runtime_version: None,
    };
    let (settings, advisories) = Settings::from_parts(content, stage, &ctx)
        .map_err(|e| PocketError::Config(e.to_string()))?;
    if settings.general.project_name.is_empty() {
        require_project_name(None)?;
    }
    for advisory in advisories {
        tracing::warn!("{}", advisory.message);
    }
    Ok(settings)
}

/// `{stage}` / `{project}` / `{namespace}` の展開 (Python の `format_vars`)
fn format_vars(s: &str, stage: &str, project_name: &str, namespace: &str) -> String {
    s.replace("{stage}", stage)
        .replace("{project}", project_name)
        .replace("{namespace}", namespace)
}

/// `[cloudfront.<name>]` の name 一覧 (sorted) と object_hash route 一覧
/// (cloudfront は toml に書いた順、route は定義順)
fn static_routes(settings: &Settings) -> (Vec<String>, Vec<StaticRouteConfig>) {
    let mut names: Vec<String> = settings.cloudfront.keys().cloned().collect();
    names.sort();
    let mut routes = Vec::new();
    for (name, cf) in settings.cloudfront.iter() {
        for r in &cf.routes {
            if r.versioning != Some(settings_model::Versioning::ObjectHash) {
                continue;
            }
            let route_name = route_logical_name(&r.path_pattern);
            let map_path = r
                .map
                .clone()
                .unwrap_or_else(|| format!("pocket_static/{name}-{route_name}.map"));
            routes.push(StaticRouteConfig {
                cloudfront: name.clone(),
                cloudfront_domain: cf.domain.clone(),
                route_ref: Some(r.r#ref.clone()).filter(|r| !r.is_empty()),
                url_prefix: route_url_prefix(&r.path_pattern),
                meta_prefix: format!(".pocket/static/{name}/{route_name}/"),
                key_prefix: route_key_prefix(
                    r.origin_path.as_deref().unwrap_or(""),
                    &r.path_pattern,
                ),
                map_path,
                versioned_max_age: r.versioned_max_age.max(0) as u64,
                sources: r
                    .sources
                    .iter()
                    .map(|s| StaticSourceConfig {
                        name: s.name.clone(),
                        dir: s.dir.clone(),
                        prefix: s.prefix.clone(),
                        include: s.include.clone(),
                        exclude: s.exclude.clone(),
                        hashed: s.hashed,
                    })
                    .collect(),
                route_name,
            });
        }
    }
    (names, routes)
}

/// `[s3]` の bucket 名 (Python の `S3.bucket_name(format_vars)` と同じ展開)
fn s3_bucket_name(settings: &Settings, stage: &str) -> Option<String> {
    settings.s3.as_ref().map(|s3| {
        format_vars(
            s3.bucket_name_format.as_str(),
            stage,
            &settings.general.project_name,
            &settings.general.namespace,
        )
    })
}

/// pocket.toml のうち `pocket-rs static` が使う部分を読む (container を選ばない)
pub fn load_static_config_from_path(path: &Path, stage: &str) -> Result<StaticConfig> {
    let content = std::fs::read_to_string(path).map_err(PocketError::Io)?;
    load_static_config_from_str(&content, stage)
}

/// TOML 文字列から [`StaticConfig`] を構築する
pub fn load_static_config_from_str(content: &str, stage: &str) -> Result<StaticConfig> {
    let settings = load_settings(content, stage)?;
    let (cloudfront_names, static_routes) = static_routes(&settings);
    let s3_bucket_name = s3_bucket_name(&settings, stage);
    Ok(StaticConfig {
        region: settings.general.region,
        project_name: settings.general.project_name,
        stage: stage.to_string(),
        cloudfront_names,
        s3_bucket_name,
        static_routes,
    })
}

fn store_type(s: settings_model::StoreType) -> StoreType {
    match s {
        settings_model::StoreType::Sm => StoreType::Sm,
        settings_model::StoreType::Ssm => StoreType::Ssm,
    }
}

/// enum の値を toml / JSON に書く文字列で取り出す (`password` 等)
fn serde_name<T: serde::Serialize>(value: &T) -> String {
    serde_json::to_value(value)
        .ok()
        .and_then(|v| v.as_str().map(str::to_string))
        .unwrap_or_default()
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
    let settings = load_settings(content, stage)?;
    pocket_config_from_settings(&settings, stage, container)
}

/// [`Settings`] から runtime が使う [`PocketConfig`] を導出する
fn pocket_config_from_settings(
    settings: &Settings,
    stage: &str,
    container: Option<&str>,
) -> Result<PocketConfig> {
    let general = &settings.general;
    let project_name = general.project_name.clone();
    let format_vars =
        |s: &str| -> String { format_vars(s, stage, &project_name, &general.namespace) };
    let resource_prefix = format_vars(&general.prefix_template);
    let slug = settings.slug();

    // 自 container の選択 (POCKET_CONTAINER env > 単一 container)
    let container_name = match container {
        Some(name) => {
            if !settings.container.contains_key(name) {
                return Err(PocketError::Config(format!(
                    "container `{name}` は設定にありません"
                )));
            }
            name.to_string()
        }
        None => match settings.container.len() {
            0 => String::new(),
            1 => settings
                .container
                .keys()
                .next()
                .cloned()
                .unwrap_or_default(),
            _ => {
                return Err(PocketError::Config(
                    "複数の container が定義されています。POCKET_CONTAINER \
                     環境変数で自 container を指定してください。"
                        .into(),
                ));
            }
        },
    };

    let mut containers_config: HashMap<String, HashMap<String, HandlerConfig>> = HashMap::new();
    for (c_name, c) in settings.container.iter() {
        let handlers = c
            .handlers
            .iter()
            .map(|(key, h)| {
                let apigateway = h.apigateway.as_ref().map(|ag| ApiGatewayConfig {
                    domain: ag.domain.clone(),
                });
                // Python 側 SqsContext.from_settings と同じ導出: {prefix}{container}-{handler}
                let sqs = h.sqs.as_ref().map(|_| SqsConfig {
                    name: format!("{resource_prefix}{c_name}-{key}"),
                });
                (key.clone(), HandlerConfig { apigateway, sqs })
            })
            .collect();
        containers_config.insert(c_name.clone(), handlers);
    }
    let handlers_config = containers_config
        .get(&container_name)
        .cloned()
        .unwrap_or_default();

    let (cloudfront_names, static_routes) = static_routes(settings);
    let s3_bucket_name = s3_bucket_name(settings, stage);

    // 自動注入 managed secret (Python の pocket.context._injected_managed_specs と
    // 同じ導出)。enable_origin_verify の検証用 secret と waf.allow_rules の header
    // secret は shared store の managed 扱いで runtime env に載せる
    let mut injected: Vec<(String, ManagedSecretSpec)> = Vec::new();
    if settings.cloudfront.values().any(|c| c.enable_origin_verify) {
        injected.push((
            ORIGIN_VERIFY_SECRET_KEY.to_string(),
            ManagedSecretSpec {
                secret_type: "origin_verify_secret".to_string(),
                options: HashMap::new(),
            },
        ));
    }
    let waf_headers: BTreeSet<String> = settings
        .cloudfront
        .values()
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
    // 継承 > 既定値 (Python の project_secrets_base 相当。全 container での一致は
    // 検証済み)。自 container に secrets 宣言が無くても、自動注入があれば shared
    // view を作る。
    let own_secrets = settings
        .container
        .get(&container_name)
        .and_then(|c| c.secrets.as_ref());
    let base = own_secrets.or_else(|| settings.container.values().find_map(|c| c.secrets.as_ref()));
    let (store, base_format) = match base {
        Some(sc) => (
            store_type(sc.store),
            sc.pocket_key_format.as_str().to_string(),
        ),
        None => (StoreType::Sm, "{stage}-{project}-{namespace}".to_string()),
    };
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
    if let Some(sc) = own_secrets {
        for (k, v) in sc.managed.iter() {
            let options = v
                .options
                .iter()
                .map(|(ok, ov)| {
                    let s = match ov {
                        magic_pocket_settings::types::StrOrInt::Str(s) => s.clone(),
                        magic_pocket_settings::types::StrOrInt::Int(i) => i.to_string(),
                    };
                    (ok.clone(), s)
                })
                .collect();
            let spec = ManagedSecretSpec {
                secret_type: serde_name(&v.r#type),
                options,
            };
            if v.shared {
                shared_managed.insert(k.clone(), spec);
            } else {
                unshared_managed.insert(k.clone(), spec);
            }
        }
        for (k, v) in sc.user.iter() {
            let spec_store = v.store.map(store_type);
            // name は type 基準の正準パスへ解決してから保持する
            // (Python の SecretsContext.from_settings と同じ resolve)。
            // 正準パスは project 側の pocket_key から導出する。
            // name と type の排他・どちらか必須は設定の検証で保証済み
            let name = match (&v.name, &v.r#type) {
                (Some(n), _) => format_vars(n),
                (None, Some(t)) => {
                    let effective_store = spec_store.clone().unwrap_or(store.clone());
                    user_secret_path(&project_key, &serde_name(t), &effective_store)
                }
                (None, None) => unreachable!("UserSecretSpec は name か type を持つ"),
            };
            user.insert(
                k.clone(),
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
        region: general.region.clone(),
        project_name: project_name.clone(),
        namespace: general.namespace.clone(),
        prefix_template: general.prefix_template.clone(),
        stage: stage.to_string(),
        slug,
        resource_prefix,
        container_name,
        secrets: secrets_config,
        shared_secrets: shared_secrets_config,
        handlers: handlers_config,
        containers: containers_config,
        cloudfront_names,
        s3_bucket_name,
        static_routes,
    })
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
sqs = { dead_letter_alert = { enabled = false } }
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
sqs = { dead_letter_alert = { enabled = false } }
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
routes = [{ is_default = true, origin_path = "/spa" }]

[cloudfront.admin]
routes = [{ is_default = true, origin_path = "/admin" }]
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
        assert!(config.s3_bucket_name.is_none());
        assert!(config.static_routes.is_empty());
    }

    #[test]
    fn test_static_routes_collected_from_object_hash_routes() {
        // routes は inline table でも [[...]] でも来る。object_hash 以外は無視する。
        // 並びは toml に書いた順 (cloudfront web → media)
        let toml = r#"
[general]
region = "ap-northeast-1"
project_name = "myapp"
stages = ["dev"]

[s3]
bucket_name_format = "{stage}-{project}-assets"

[cloudfront.web]
domain = "example.com"
routes = [
  { is_default = true, is_spa = true, origin_path = "/spa" },
  { path_pattern = "/static/*", ref = "static", versioning = "object_hash", sources = [{ name = "data", dir = "data" }] },
  { path_pattern = "/assets/*", versioning = "content_hash", upload_dir = "build" },
]

[cloudfront.media]

[[cloudfront.media.routes]]
is_default = true
origin_path = "/media"

[[cloudfront.media.routes]]
path_pattern = "/img/v2/*"
versioning = "object_hash"
sources = [{ name = "img", dir = "img" }]
"#;
        let config = load_config_from_str(toml, "dev", None).unwrap();
        assert_eq!(config.s3_bucket_name.as_deref(), Some("dev-myapp-assets"));
        assert_eq!(
            config.static_routes,
            vec![
                StaticRouteConfig {
                    cloudfront: "web".into(),
                    cloudfront_domain: Some("example.com".into()),
                    route_ref: Some("static".into()),
                    route_name: "static".into(),
                    url_prefix: "/static/".into(),
                    meta_prefix: ".pocket/static/web/static/".into(),
                    key_prefix: "static/".into(),
                    map_path: "pocket_static/web-static.map".into(),
                    versioned_max_age: 31_536_000,
                    sources: vec![StaticSourceConfig {
                        name: "data".into(),
                        dir: "data".into(),
                        prefix: String::new(),
                        include: vec![],
                        exclude: vec![],
                        hashed: true,
                    }],
                },
                StaticRouteConfig {
                    cloudfront: "media".into(),
                    cloudfront_domain: None,
                    route_ref: None,
                    route_name: "img-v2".into(),
                    url_prefix: "/img/v2/".into(),
                    meta_prefix: ".pocket/static/media/img-v2/".into(),
                    key_prefix: "img/v2/".into(),
                    map_path: "pocket_static/media-img-v2.map".into(),
                    versioned_max_age: 31_536_000,
                    sources: vec![StaticSourceConfig {
                        name: "img".into(),
                        dir: "img".into(),
                        prefix: String::new(),
                        include: vec![],
                        exclude: vec![],
                        hashed: true,
                    }],
                },
            ]
        );
    }

    #[test]
    fn test_s3_bucket_name_default_format() {
        let toml = r#"
[general]
region = "ap-northeast-1"
project_name = "myapp"
stages = ["dev"]

[s3]
"#;
        let config = load_config_from_str(toml, "dev", None).unwrap();
        // Python の S3.bucket_name_format 既定 "{stage}-{project}-{namespace}"
        assert_eq!(config.s3_bucket_name.as_deref(), Some("dev-myapp-pocket"));
    }

    #[test]
    fn test_route_logical_name_matches_python() {
        assert_eq!(route_logical_name(""), "root");
        assert_eq!(route_logical_name("/static/*"), "static");
        assert_eq!(route_logical_name("/foo-bar/*"), "foobar");
        assert_eq!(
            route_logical_name("/v2/_app/immutable/*"),
            "v2-app-immutable"
        );
        assert_eq!(route_url_prefix("/static/*"), "/static/");
        assert_eq!(route_url_prefix(""), "/");
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

[s3]

[cloudfront.web]
enable_origin_verify = true
routes = [{ is_default = true, type = "lambda", handler = "main.wsgi" }]
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

[s3]

[cloudfront.web]
enable_origin_verify = false
routes = [{ is_default = true, origin_path = "/spa" }]

[cloudfront.admin]
routes = [{ is_default = true, origin_path = "/admin" }]
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

[container.main.handlers.wsgi]
command = "app"
apigateway = {}

[s3]

[cloudfront.web]
routes = [{ is_default = true, origin_path = "/spa" }]

[cloudfront.web.waf]
allow_rules = [
    { header = "ZULU_TOKEN" },
    { path = "/healthz" },
    { path = "/smoke", header = "ALPHA_TOKEN" },
]

[cloudfront.admin]
enable_origin_verify = true
routes = [{ is_default = true, type = "lambda", handler = "main.wsgi" }]

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

[container.v2.handlers.wsgi]
command = "app"
apigateway = {}

[s3]

[cloudfront.web]
enable_origin_verify = true
routes = [{ is_default = true, type = "lambda", handler = "v2.wsgi" }]
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

[container.main.handlers.wsgi]
command = "app"
apigateway = {}

[s3]

[cloudfront.web]
enable_origin_verify = true
routes = [{ is_default = true, type = "lambda", handler = "main.wsgi" }]
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
project_name = "myapp-neon"
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
        assert!(err.to_string().contains("排他"), "got {err}");
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
}
