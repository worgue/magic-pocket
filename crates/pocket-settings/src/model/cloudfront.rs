//! `[cloudfront.<name>]` 一式 (Python の `CloudFront` / `Route` / `StaticSource` /
//! `RouteBuild` / `RedirectFrom` / `CloudFrontWaf*`)。

use std::sync::OnceLock;

use regex::Regex;
use serde::{Deserialize, Serialize};

use crate::error::{child, PathSeg, SettingsError};
use crate::model::general::default_true;
use crate::types::py_repr;

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RedirectFrom {
    pub domain: String,
    #[serde(default)]
    pub hosted_zone_id_override: Option<String>,
}

/// `versioning = "object_hash"` の route に載せるローカルファイルの集合
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct StaticSource {
    pub name: String,
    pub dir: String,
    #[serde(default)]
    pub prefix: String,
    #[serde(default)]
    pub include: Vec<String>,
    #[serde(default)]
    pub exclude: Vec<String>,
    #[serde(default = "default_true")]
    pub hashed: bool,
}

impl StaticSource {
    fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        let err = |m: String| Err(SettingsError::value(path.to_vec(), m));
        static RE: OnceLock<Regex> = OnceLock::new();
        let re = RE.get_or_init(|| Regex::new("^[A-Za-z0-9_-]+$").expect("re"));
        if !re.is_match(&self.name) {
            return err(format!(
                "sources の name は英数字と - _ だけで書いてください: {}",
                py_repr(&self.name)
            ));
        }
        if self.dir.is_empty() || self.dir.starts_with('/') {
            return err(format!(
                "sources の dir は pocket.toml からの相対パスで書いてください: {}",
                py_repr(&self.dir)
            ));
        }
        if self.prefix.starts_with('/') {
            return err(format!(
                "sources の prefix は先頭の / を付けずに書いてください: {}",
                py_repr(&self.prefix)
            ));
        }
        if !self.prefix.is_empty() && !self.prefix.ends_with('/') {
            return err(format!(
                "sources の prefix は末尾を / にしてください (例: 'img/'): {}",
                py_repr(&self.prefix)
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RouteBuild {
    pub dir: String,
    pub cmd: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum RouteType {
    #[default]
    S3,
    Lambda,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Versioning {
    ContentHash,
    DeployHash,
    ObjectHash,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Route {
    #[serde(default)]
    pub r#type: RouteType,
    #[serde(default)]
    pub handler: Option<String>,
    #[serde(default)]
    pub path_pattern: String,
    #[serde(default)]
    pub is_default: bool,
    #[serde(default)]
    pub is_spa: bool,
    #[serde(default)]
    pub versioning: Option<Versioning>,
    #[serde(default = "default_spa_fallback_html")]
    pub spa_fallback_html: String,
    #[serde(default = "default_versioned_max_age")]
    pub versioned_max_age: i64,
    #[serde(default)]
    pub r#ref: String,
    #[serde(default)]
    pub signed: bool,
    #[serde(default)]
    pub build: Option<RouteBuild>,
    #[serde(default)]
    pub upload_dir: Option<String>,
    /// None は versioning で決める (content_hash は消さない、それ以外は消す)
    #[serde(default)]
    pub delete_stale: Option<bool>,
    #[serde(default)]
    pub origin_path: Option<String>,
    #[serde(default)]
    pub require_token: bool,
    #[serde(default = "default_login_path")]
    pub login_path: String,
    #[serde(default)]
    pub sources: Vec<StaticSource>,
    #[serde(default)]
    pub map: Option<String>,
}

fn default_spa_fallback_html() -> String {
    "index.html".to_string()
}
fn default_versioned_max_age() -> i64 {
    60 * 60 * 24 * 365
}
fn default_login_path() -> String {
    "/api/auth/login".to_string()
}

/// Python の `s.rstrip("/*").lstrip("/")` (path_pattern の prefix 部分)
fn pattern_prefix(path_pattern: &str) -> &str {
    path_pattern
        .trim_end_matches(['/', '*'])
        .trim_start_matches('/')
}

impl Route {
    fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        for (i, source) in self.sources.iter().enumerate() {
            let mut p = child(path, "sources");
            p.push(PathSeg::Index(i));
            source.validate(&p)?;
        }
        let err = |m: &str| Err(SettingsError::value(path.to_vec(), m));
        // check_origin_path
        if self.r#type == RouteType::Lambda {
            if self.origin_path.is_some() {
                return err("type = 'lambda' cannot use origin_path");
            }
        } else {
            match self.origin_path.as_deref() {
                None | Some("") => {
                    if pattern_prefix(&self.path_pattern).is_empty() {
                        return err(
                            "S3 route requires `origin_path` when path_pattern has no prefix \
                             (catch-all). Set a prefix like \"/spa\" to separate from other S3 \
                             routes (e.g. Django static at /static).\n  Example: { type = \
                             \"s3\", path_pattern = \"\", is_default = true, origin_path = \
                             \"/spa\" }",
                        );
                    }
                }
                Some(origin_path) => {
                    if origin_path == "/" {
                        return err(
                            "origin_path = \"/\" (バケット直下の配信) はサポートしていません。 \
                             pocket は 1 つの S3 バケットを複数 route で共有するため、\
                             バケット直下に向けると OAC のバケットポリシーがバケット全体の\
                             許可になり、route を張っていない prefix のオブジェクトまで\
                             CDN 経由で到達可能になります。\n  prefix を与えてください。\
                             URL 上のパスは path_pattern で決まるため、配信結果は変わりません \
                             (例: origin_path = \"/spa\")。",
                        );
                    }
                    if !origin_path.starts_with('/') {
                        return err("origin_path must starts with /");
                    }
                    if origin_path.ends_with('/') {
                        return err("origin_path must not ends with /");
                    }
                }
            }
        }
        // check_require_token
        if self.require_token && !self.is_spa {
            return err("require_token=True requires is_spa=True");
        }
        // check_lambda_route
        if self.r#type == RouteType::Lambda {
            if self.handler.as_deref().unwrap_or("").is_empty() {
                return err("handler is required when type = 'lambda'");
            }
            if self.is_spa || self.versioning.is_some() || self.signed || self.require_token {
                return err(
                    "type = 'lambda' cannot use is_spa, versioning, signed, or require_token",
                );
            }
            if self.build.is_some() || self.upload_dir.as_deref().is_some_and(|u| !u.is_empty()) {
                return err("type = 'lambda' cannot use build or upload_dir");
            }
        }
        if self.handler.as_deref().is_some_and(|h| !h.is_empty())
            && self.r#type != RouteType::Lambda
        {
            return err("handler requires type = 'lambda'");
        }
        let has_upload_dir = self.upload_dir.as_deref().is_some_and(|u| !u.is_empty());
        // check_build
        if self.build.is_some() && has_upload_dir {
            return err(
                "build と upload_dir は同時に設定できません。pocket にビルド\
                 させるなら build、外部でビルドした成果物をアップロードするだけ\
                 なら upload_dir を選んでください",
            );
        }
        // check_delete_stale
        if self.delete_stale.is_some() && !(self.build.is_some() || has_upload_dir) {
            return err(
                "delete_stale は build か upload_dir を持つ route でだけ指定できます \
                 (アップロードしない route には消す対象がありません)",
            );
        }
        // check_flags
        if self.is_spa && self.versioning.is_some() {
            return err("is_spa と versioning は同時に設定できません");
        }
        // check_object_hash
        if self.versioning == Some(Versioning::ObjectHash) {
            if self.sources.is_empty() {
                return err(
                    "versioning = \"object_hash\" には sources を 1 つ以上宣言して\
                     ください (例: sources = [{ name = \"data\", dir = \"data\" }])",
                );
            }
            if self.build.is_some() || has_upload_dir {
                return err(
                    "versioning = \"object_hash\" と build / upload_dir は同時に\
                     設定できません (object_hash は pocket-rs static publish で\
                     上げます)",
                );
            }
            if self.signed {
                return err("versioning = \"object_hash\" と signed は同時に設定できません");
            }
            let names: Vec<&str> = self.sources.iter().map(|s| s.name.as_str()).collect();
            let mut dupes: Vec<&str> = names
                .iter()
                .copied()
                .filter(|n| names.iter().filter(|m| *m == n).count() > 1)
                .collect();
            dupes.sort();
            dupes.dedup();
            if !dupes.is_empty() {
                return Err(SettingsError::value(
                    path.to_vec(),
                    format!("sources の name が重複しています: {}", dupes.join(", ")),
                ));
            }
        } else {
            if !self.sources.is_empty() {
                return err("sources は versioning = \"object_hash\" の route でだけ使えます");
            }
            if self.map.is_some() {
                return err("map は versioning = \"object_hash\" の route でだけ使えます");
            }
        }
        // check_is_default
        if self.is_default && !self.path_pattern.is_empty() {
            return err("is_default route must have empty path_pattern");
        }
        if !self.is_default && self.path_pattern.is_empty() {
            return err("route with empty path_pattern must have is_default = true");
        }
        // check_path_pattern
        if !self.path_pattern.is_empty() {
            if !self.path_pattern.starts_with('/') {
                return err("non default path_pattern must starts with /");
            }
            if self.path_pattern.ends_with('/') {
                return err("path_pattern must not ends with /");
            }
            if self.path_pattern.starts_with("/.pocket") {
                return err("path_pattern に /.pocket で始まる値は使えません \
                     (pocket の内部 metadata の置き場です)");
            }
        }
        // check_ref
        if !self.r#ref.is_empty() && !self.path_pattern.ends_with("/*") {
            return err("When ref is set, path_pattern must end with /*");
        }
        Ok(())
    }

    /// origin_path が path_pattern の prefix と同じで S3 key が二重になる場合の助言
    pub fn double_prefix_advisory(&self) -> Option<String> {
        let origin_path = self.origin_path.as_deref().filter(|o| !o.is_empty())?;
        if self.r#type != RouteType::S3 {
            return None;
        }
        let prefix = pattern_prefix(&self.path_pattern);
        if prefix.is_empty() || origin_path.trim_matches('/') != prefix {
            return None;
        }
        Some(format!(
            "S3 route origin_path = \"{origin_path}\" が path_pattern = \"{}\" と重複し、\
             S3 key が 二重 prefix ({prefix}/{prefix}/...) になります。 origin_path を\
             省略すると単一 prefix ({prefix}/...) になり、S3 を直接操作する運用で \
             prefix が直感的になります。",
            self.path_pattern
        ))
    }
}

/// WAF の path に許す文字 (RFC 3986 の pchar + "/" から '"' と '\\' を除いたもの)
const WAF_PATH_SAFE_CHARS: &str =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789/-._~!$&'()+,;=:@%";
/// ByteMatchStatement.SearchString の上限 (bytes)
const WAF_PATH_MAX_BYTES: usize = 200;
/// rate-based rule が受け付ける集計窓 (秒)
const WAF_RATE_LIMIT_WINDOWS: [i64; 4] = [60, 120, 300, 600];
/// WebACL あたりの rate-based rule 数
const WAF_RATE_LIMIT_MAX_RULES: usize = 10;

fn validate_waf_path(path: &str, field: &str) -> Result<(), String> {
    if !path.starts_with('/') {
        return Err(format!("{field}: path は '/' で始めてください: {path}"));
    }
    let without_last = &path[..path.len() - path.chars().last().map_or(0, char::len_utf8)];
    if without_last.contains('*') {
        return Err(format!(
            "{field}: path の '*' は末尾 (prefix 一致) のみ使用できます: {path}"
        ));
    }
    let body = path.strip_suffix('*').unwrap_or(path);
    let mut bad: Vec<char> = body
        .chars()
        .filter(|c| !WAF_PATH_SAFE_CHARS.contains(*c))
        .collect();
    bad.sort();
    bad.dedup();
    if !bad.is_empty() {
        let bad: String = bad.into_iter().collect();
        return Err(format!(
            "{field}: path に使えない文字が含まれています {bad}: {path}"
        ));
    }
    if body.len() > WAF_PATH_MAX_BYTES {
        return Err(format!(
            "{field}: path は ('*' を除いて) {WAF_PATH_MAX_BYTES} bytes 以内にしてください \
             ({} bytes): {path}",
            body.len()
        ));
    }
    Ok(())
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CloudFrontWafAllowRule {
    #[serde(default)]
    pub path: Option<String>,
    /// managed secret のキー名 (Python の `EnvStr`)
    #[serde(default)]
    pub header: Option<String>,
}

impl CloudFrontWafAllowRule {
    fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        if let Some(header) = &self.header {
            crate::model::container::check_env_str(header, &child(path, "header"))?;
        }
        if self.path.is_none() && self.header.is_none() {
            return Err(SettingsError::value(
                path.to_vec(),
                "allow_rules: path か header の少なくとも一方を指定してください。",
            ));
        }
        if let Some(p) = &self.path {
            validate_waf_path(p, "allow_rules")
                .map_err(|m| SettingsError::value(path.to_vec(), m))?;
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum RateLimitAction {
    #[default]
    Block,
    Count,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CloudFrontWafRateLimit {
    #[serde(deserialize_with = "crate::types::waf_limit")]
    pub limit: i64,
    #[serde(default = "default_window_sec")]
    pub window_sec: i64,
    #[serde(default)]
    pub action: RateLimitAction,
    #[serde(default)]
    pub path: Option<String>,
}

fn default_window_sec() -> i64 {
    300
}

impl CloudFrontWafRateLimit {
    fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        if !WAF_RATE_LIMIT_WINDOWS.contains(&self.window_sec) {
            let windows: Vec<String> = WAF_RATE_LIMIT_WINDOWS.iter().map(i64::to_string).collect();
            return Err(SettingsError::value(
                path.to_vec(),
                format!(
                    "rate_limits: window_sec は {} のいずれかにしてください: {}",
                    windows.join(" / "),
                    self.window_sec
                ),
            ));
        }
        if let Some(p) = &self.path {
            validate_waf_path(p, "rate_limits")
                .map_err(|m| SettingsError::value(path.to_vec(), m))?;
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CloudFrontWaf {
    #[serde(default = "default_true")]
    pub enable_ip_set: bool,
    #[serde(default)]
    pub managed_rule_groups: Vec<String>,
    #[serde(default)]
    pub allow_rules: Vec<CloudFrontWafAllowRule>,
    /// 宣言順 = 評価順
    #[serde(default)]
    pub rate_limits: Vec<CloudFrontWafRateLimit>,
}

impl CloudFrontWaf {
    fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        for (i, r) in self.allow_rules.iter().enumerate() {
            let mut p = child(path, "allow_rules");
            p.push(PathSeg::Index(i));
            r.validate(&p)?;
        }
        for (i, r) in self.rate_limits.iter().enumerate() {
            let mut p = child(path, "rate_limits");
            p.push(PathSeg::Index(i));
            r.validate(&p)?;
        }
        if self.rate_limits.len() > WAF_RATE_LIMIT_MAX_RULES {
            return Err(SettingsError::value(
                path.to_vec(),
                format!(
                    "[cloudfront.<name>.waf]: rate_limits は {WAF_RATE_LIMIT_MAX_RULES} 件までです \
                     (AWS WAF の WebACL あたりの rate-based rule 数の上限): {} 件",
                    self.rate_limits.len()
                ),
            ));
        }
        let blocking = self
            .rate_limits
            .iter()
            .any(|r| r.action == RateLimitAction::Block);
        if !self.enable_ip_set && self.managed_rule_groups.is_empty() && !blocking {
            return Err(SettingsError::value(
                path.to_vec(),
                "[cloudfront.<name>.waf]: enable_ip_set = false の場合は \
                 managed_rule_groups か action = \"block\" の rate_limits を \
                 1 つ以上指定してください。どれも無いと WebACL が何も遮断しない \
                 pass-through 状態になり、WAF を attach する意味がありません \
                 (action = \"count\" の rate_limits は観測だけで遮断しません)。",
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CloudFront {
    #[serde(default)]
    pub domain: Option<String>,
    #[serde(default)]
    pub hosted_zone_id_override: Option<String>,
    #[serde(default)]
    pub redirect_from: Vec<RedirectFrom>,
    #[serde(default)]
    pub routes: Vec<Route>,
    #[serde(default)]
    pub signing_key: Option<String>,
    #[serde(default)]
    pub token_secret: Option<String>,
    /// distribution 全体に Basic 認証を掛ける (managed secret のキー名)
    #[serde(default)]
    pub basic_auth: Option<String>,
    #[serde(default)]
    pub managed_assets: Option<String>,
    #[serde(default)]
    pub waf: Option<CloudFrontWaf>,
    #[serde(default)]
    pub enable_origin_verify: bool,
}

/// route の path_pattern から CFn 論理 ID 用の名前を導出する (Python の
/// `route_logical_name`。pocket-rs の `config::route_logical_name` と同じ規則)
pub fn route_logical_name(path_pattern: &str) -> String {
    if path_pattern.is_empty() {
        return "root".to_string();
    }
    path_pattern
        .split('/')
        .filter(|p| !p.is_empty() && *p != "*")
        .map(|p| {
            p.chars()
                .filter(|c| c.is_alphanumeric())
                .collect::<String>()
        })
        .collect::<Vec<_>>()
        .join("-")
}

/// 非英数字を境界にした CamelCase (Python の `camel_logical_name`)
fn camel_logical_name(key: &str) -> String {
    key.split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|p| !p.is_empty())
        .map(|p| {
            let mut chars = p.chars();
            match chars.next() {
                Some(first) => first
                    .to_uppercase()
                    .chain(chars.flat_map(char::to_lowercase))
                    .collect(),
                None => String::new(),
            }
        })
        .collect()
}

impl CloudFront {
    pub(crate) fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        for (i, r) in self.routes.iter().enumerate() {
            let mut p = child(path, "routes");
            p.push(PathSeg::Index(i));
            r.validate(&p)?;
        }
        if let Some(waf) = &self.waf {
            waf.validate(&child(path, "waf"))?;
        }
        let err = |m: String| Err(SettingsError::value(path.to_vec(), m));
        // check_domain_redirect_from
        if self.domain.is_none() && !self.redirect_from.is_empty() {
            return err("redirect_from requires domain to be set".to_string());
        }
        // check_logical_id_uniqueness
        let mut seen_routes: Vec<(String, &str)> = Vec::new();
        for route in &self.routes {
            let name = route_logical_name(&route.path_pattern);
            if name.trim_matches('-').is_empty() {
                return err(format!(
                    "path_pattern '{}' から CloudFormation 論理 ID を導出できません。\
                     catch-all は path_pattern = \"\" (is_default = true) を使ってください。",
                    route.path_pattern
                ));
            }
            if let Some((_, first)) = seen_routes.iter().find(|(n, _)| *n == name) {
                return err(format!(
                    "routes '{first}' と '{}' は同じ CloudFormation 論理 ID '{name}' を\
                     導出します (英数字以外は除去されます)。区別可能な path prefix を\
                     使ってください。",
                    route.path_pattern
                ));
            }
            seen_routes.push((name, &route.path_pattern));
        }
        let mut seen_domains: Vec<(String, &str)> = Vec::new();
        for rf in &self.redirect_from {
            let key = camel_logical_name(&rf.domain);
            if let Some((_, first)) = seen_domains.iter().find(|(k, _)| *k == key) {
                return err(format!(
                    "redirect_from '{first}' と '{}' は同じ CloudFormation 論理 ID \
                     '{key}' を導出します。",
                    rf.domain
                ));
            }
            seen_domains.push((key, &rf.domain));
        }
        // check_token_secret
        let has_require_token = self.routes.iter().any(|r| r.require_token);
        if has_require_token && self.token_secret.as_deref().unwrap_or("").is_empty() {
            return err("token_secret is required when route has require_token=true".to_string());
        }
        // check_routes
        if self.routes.is_empty() {
            return err("routes must have at least one route".to_string());
        }
        if self.routes.iter().filter(|r| r.is_default).count() != 1 {
            return err("routes must have exactly one is_default = true route".to_string());
        }
        // check_route_ref_unique
        let mut refs: Vec<&str> = Vec::new();
        for route in self.routes.iter().filter(|r| !r.r#ref.is_empty()) {
            if refs.contains(&route.r#ref.as_str()) {
                return err(format!(
                    "routes の ref '{}' が重複しています。ref は route を一意に識別するため、\
                     同じ値を複数の route に設定することはできません。",
                    route.r#ref
                ));
            }
            refs.push(&route.r#ref);
        }
        // check_route_s3_prefix_overlap
        let prefixes: Vec<(&str, String)> = self
            .routes
            .iter()
            .filter(|r| r.r#type == RouteType::S3)
            .map(|r| {
                let label = if !r.r#ref.is_empty() {
                    r.r#ref.as_str()
                } else if !r.path_pattern.is_empty() {
                    r.path_pattern.as_str()
                } else {
                    "default"
                };
                let origin = r.origin_path.as_deref().unwrap_or("");
                let joined = format!("{origin}{}", r.path_pattern.trim_end_matches(['/', '*']));
                (label, joined.trim_start_matches('/').to_string())
            })
            .collect();
        let child_msg = |la: &str, pa: &str, lb: &str, pb: &str| {
            format!(
                "ルート '{la}' の S3 prefix '{pa}' はルート '{lb}' の S3 prefix '{pb}' \
                 の子パスです。s3 sync --delete 等で意図せずファイルが削除される危険があります。"
            )
        };
        for (i, (label_a, prefix_a)) in prefixes.iter().enumerate() {
            for (label_b, prefix_b) in &prefixes[i + 1..] {
                if prefix_a == prefix_b {
                    return err(format!(
                        "ルート '{label_a}' と '{label_b}' の S3 prefix が同一です: '{prefix_a}'"
                    ));
                }
                if prefix_b.starts_with(&format!("{prefix_a}/")) {
                    return err(child_msg(label_b, prefix_b, label_a, prefix_a));
                }
                if prefix_a.starts_with(&format!("{prefix_b}/")) {
                    return err(child_msg(label_a, prefix_a, label_b, prefix_b));
                }
            }
        }
        Ok(())
    }
}
