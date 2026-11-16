//! `CloudFrontContext` とその部品 (`pocket/context.py` の後半)

use serde::Serialize;

use crate::model::{RateLimitAction, RouteType, StaticSource, Versioning};
use crate::types::OrderedMap;

#[derive(Debug, Clone, Serialize)]
pub struct RedirectFromContext {
    pub domain: String,
    pub hosted_zone_id_override: Option<String>,
    // computed
    pub yaml_key: String,
    pub hosted_zone_id: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct RouteContext {
    pub r#type: RouteType,
    pub handler: Option<String>,
    pub path_pattern: String,
    pub is_default: bool,
    pub is_spa: bool,
    pub versioning: Option<Versioning>,
    pub spa_fallback_html: String,
    pub versioned_max_age: i64,
    pub r#ref: String,
    pub signed: bool,
    pub build_cmd: Option<String>,
    pub upload_dir: Option<String>,
    pub delete_stale: Option<bool>,
    pub origin_path: String,
    pub require_token: bool,
    pub login_path: String,
    pub cloudfront_name: String,
    pub sources: Vec<StaticSource>,
    pub map: Option<String>,
    // computed
    pub is_lambda: bool,
    pub is_content_hash: bool,
    pub is_deploy_hash: bool,
    pub is_object_hash: bool,
    pub key_prefix: String,
    pub url_prefix: String,
    pub static_meta_prefix: String,
    pub map_path: String,
    pub name: String,
    pub yaml_key: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct CloudFrontWafAllowRuleContext {
    pub path: Option<String>,
    pub header: Option<String>,
    // computed
    pub path_positional_constraint: String,
    pub path_search_string: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct CloudFrontWafRateLimitContext {
    pub path: Option<String>,
    pub limit: i64,
    pub window_sec: i64,
    pub action: RateLimitAction,
    // computed
    pub path_positional_constraint: String,
    pub path_search_string: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct CloudFrontWafContext {
    pub enable_ip_set: bool,
    pub managed_rule_groups: Vec<String>,
    pub allow_rules: Vec<CloudFrontWafAllowRuleContext>,
    pub rate_limits: Vec<CloudFrontWafRateLimitContext>,
    // computed
    pub header_secret_keys: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct CloudFrontContext {
    pub name: String,
    pub region: String,
    pub s3_region: String,
    pub stage: String,
    pub domain: Option<String>,
    pub hosted_zone_id_override: Option<String>,
    pub slug: String,
    pub bucket_name: String,
    pub resource_prefix: String,
    pub redirect_from: Vec<RedirectFromContext>,
    pub routes: Vec<RouteContext>,
    pub signing_key: Option<String>,
    pub token_secret: Option<String>,
    pub basic_auth: Option<String>,
    pub api_origins: OrderedMap<String>,
    pub managed_assets: Option<String>,
    pub deploy_hash: String,
    pub waf: Option<CloudFrontWafContext>,
    pub enable_origin_verify: bool,
    // computed
    pub yaml_key: String,
    pub bucket_policy_prefix: String,
    pub default_route: RouteContext,
    pub uploadable_routes: Vec<RouteContext>,
    pub extra_s3_routes: Vec<RouteContext>,
    pub extra_lambda_routes: Vec<RouteContext>,
    pub has_lambda_route: bool,
    pub has_redirect_from: bool,
    pub managed_asset_files: Vec<String>,
    pub hosted_zone_id: Option<String>,
}

/// 末尾 `*` は prefix 一致、それ以外は完全一致 (`_CloudFrontWafPathMatch`)
pub(crate) fn waf_path_match(path: Option<&str>) -> (String, String) {
    let constraint = match path {
        Some(p) if p.ends_with('*') => "STARTS_WITH",
        _ => "EXACTLY",
    };
    let search = match path {
        None | Some("") => String::new(),
        Some(p) => p.strip_suffix('*').unwrap_or(p).to_string(),
    };
    (constraint.to_string(), search)
}

/// Python の `str.rstrip("/*")` (末尾の `/` と `*` を全部落とす)
pub(crate) fn rstrip_slash_star(s: &str) -> &str {
    s.trim_end_matches(['/', '*'])
}

/// `CloudFrontContext.bucket_policy_prefix`
pub(crate) fn bucket_policy_prefix(routes: &[RouteContext], managed_assets: bool) -> String {
    let mut prefixes: Vec<String> = Vec::new();
    for r in routes {
        if r.is_lambda {
            continue;
        }
        let prefix = if r.origin_path.is_empty() {
            rstrip_slash_star(&r.path_pattern).to_string()
        } else {
            r.origin_path.clone()
        };
        if !prefix.is_empty() {
            prefixes.push(prefix);
        }
    }
    if managed_assets {
        prefixes.push("/pocket_managed".to_string());
    }
    if prefixes.is_empty() {
        return String::new();
    }
    let parts: Vec<Vec<&str>> = prefixes.iter().map(|p| p.split('/').collect()).collect();
    let shortest = parts.iter().map(Vec::len).min().unwrap_or(0);
    let mut common: Vec<&str> = Vec::new();
    for i in 0..shortest {
        let seg = parts[0][i];
        if parts.iter().all(|p| p[i] == seg) {
            common.push(seg);
        } else {
            break;
        }
    }
    common.join("/")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_waf_path_match() {
        assert_eq!(
            waf_path_match(Some("/api/*")),
            ("STARTS_WITH".to_string(), "/api/".to_string())
        );
        assert_eq!(
            waf_path_match(Some("/login")),
            ("EXACTLY".to_string(), "/login".to_string())
        );
        assert_eq!(waf_path_match(None), ("EXACTLY".to_string(), String::new()));
    }

    #[test]
    fn test_rstrip_slash_star() {
        assert_eq!(rstrip_slash_star("/static/*"), "/static");
        assert_eq!(rstrip_slash_star("/*"), "");
        assert_eq!(rstrip_slash_star(""), "");
    }
}
