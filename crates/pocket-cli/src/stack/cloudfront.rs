//! CloudFront 系の stack (Python 版 `AcmStack` / `CloudFrontWafStack` /
//! `CloudFrontKeysStack` / `CloudFrontStack`)。

use std::collections::BTreeMap;

use anyhow::{bail, Result};
use magic_pocket_settings::context::{CloudFrontContext, RouteContext};
use magic_pocket_settings::pyjson;
use serde_json::{json, Map, Value as Json};

use super::render::{render_source, render_template, split_lines};
use super::{dump, variables, StackOutputs, StackTemplate};

/// ACM 証明書と WAF (Scope=CLOUDFRONT) は us-east-1 にしか作れない
const EDGE_REGION: &str = "us-east-1";

/// deploy_hash route の URL で hash とみなす要素の形 (git の short / full hash)
const DEPLOY_HASH_PATTERN: &str = "[0-9a-f]{7,40}";
/// strip Function が URI から外した hash を載せるリクエストヘッダ (キャッシュキー用)
const DEPLOY_HASH_HEADER: &str = "x-pocket-deploy-hash";

const REQUEST_LINE: &str = "var request = event.request;";

pub fn acm_stack_name(cf: &CloudFrontContext) -> String {
    format!("{}-acm", cf.slug)
}

pub fn waf_stack_name(cf: &CloudFrontContext) -> String {
    format!("{}-waf", cf.slug)
}

pub fn keys_stack_name(cf: &CloudFrontContext) -> String {
    format!("{}-cloudfront-keys", cf.slug)
}

pub fn cloudfront_stack_name(cf: &CloudFrontContext) -> String {
    format!("{}-cloudfront", cf.slug)
}

/// us-east-1 に ACM 証明書を作る stack (custom domain のとき)
pub fn acm_stack(cf: &CloudFrontContext) -> Result<StackTemplate> {
    let name = acm_stack_name(cf);
    let export = Map::new();
    let vars = variables(&name, &export, Map::new(), dump(cf));
    Ok(StackTemplate {
        body: render_template("cloudfront_acm", &vars)?,
        name,
        region: EDGE_REGION.to_string(),
        capabilities: Vec::new(),
        tags: Vec::new(),
        export,
    })
}

/// us-east-1 に WAFv2 IPSet + WebACL を作る stack。
///
/// `allow_secret_values`: allow_rules の header secret (managed secret の key → 値)。
/// template に焼き込むので template hash に効く
pub fn waf_stack(
    cf: &CloudFrontContext,
    allow_secret_values: &BTreeMap<String, String>,
) -> Result<StackTemplate> {
    let name = waf_stack_name(cf);
    let export = Map::new();
    let mut extra = Map::new();
    extra.insert("allow_secret_values".into(), json!(allow_secret_values));
    let vars = variables(&name, &export, extra, dump(cf));
    Ok(StackTemplate {
        body: render_template("cloudfront_waf", &vars)?,
        name,
        region: EDGE_REGION.to_string(),
        capabilities: Vec::new(),
        tags: Vec::new(),
        export,
    })
}

/// 署名付き URL の公開鍵と key group の stack。
///
/// `signing_public_key_pem`: managed secret から読んだ公開鍵 (未生成なら空文字)
pub fn keys_stack(cf: &CloudFrontContext, signing_public_key_pem: &str) -> Result<StackTemplate> {
    let name = keys_stack_name(cf);
    let mut export = Map::new();
    export.insert(
        "public_key_id".into(),
        json!(format!("{}-public-key-id", cf.slug)),
    );
    export.insert(
        "key_group_id".into(),
        json!(format!("{}-key-group-id", cf.slug)),
    );
    let mut extra = Map::new();
    extra.insert(
        "signing_public_key_pem".into(),
        json!(signing_public_key_pem),
    );
    let vars = variables(&name, &export, extra, dump(cf));
    Ok(StackTemplate {
        body: render_template("cloudfront_keys", &vars)?,
        name,
        region: cf.region.clone(),
        capabilities: Vec::new(),
        tags: Vec::new(),
        export,
    })
}

/// distribution の stack が外から引く値
#[derive(Debug, Clone, Default)]
pub struct CloudFrontInputs {
    /// ACM stack (us-east-1) の output。custom domain のときに要る
    pub acm_output: Option<StackOutputs>,
    /// WAF stack (us-east-1) の output。`[cloudfront.<name>.waf]` があるときに要る
    pub waf_output: Option<StackOutputs>,
    /// enable_origin_verify の secret の値 (無効・未生成なら空文字)
    pub origin_verify_secret: String,
}

/// distribution の stack
pub fn cloudfront_stack(
    cf: &CloudFrontContext,
    inputs: &CloudFrontInputs,
) -> Result<StackTemplate> {
    let name = cloudfront_stack_name(cf);
    let basic_auth = is_set(&cf.basic_auth);
    let signing_key = is_set(&cf.signing_key);
    let has_token_kvs = cf.routes.iter().any(|r| r.require_token) || basic_auth;

    let mut export = Map::new();
    if signing_key {
        export.insert(
            "key_group_id".into(),
            json!(format!("{}-key-group-id", cf.slug)),
        );
    }
    if has_token_kvs {
        export.insert(
            "kvs_arn".into(),
            json!(format!("{}-token-kvs-arn", cf.slug)),
        );
    }

    let functions = FunctionCodes::new(cf)?;
    let mut extra = Map::new();
    extra.insert("signing_key".into(), json!(signing_key));
    extra.insert("basic_auth".into(), json!(basic_auth));
    extra.insert(
        "acm_certificate_arn".into(),
        json!(acm_certificate_arn(cf, inputs)?),
    );
    extra.insert("waf_acl_arn".into(), json!(waf_acl_arn(cf, inputs)?));
    extra.insert("function_codes".into(), functions.spa()?);
    extra.insert(
        "deploy_hash_function_codes".into(),
        functions.deploy_hash()?,
    );
    extra.insert(
        "cache_control_function_codes".into(),
        functions.cache_control()?,
    );
    extra.insert("deploy_hash_header".into(), json!(DEPLOY_HASH_HEADER));
    let api_host = if cf.has_lambda_route {
        functions.api_host()?
    } else {
        String::new()
    };
    extra.insert("api_host_function_code".into(), json!(api_host));
    // basic_auth 時、素の behavior には単体 BasicAuthFunction を付ける (redirect prelude も
    // 同居)。単体 HostRedirectFunction は非 basic_auth 時のみ
    let host_redirect = if cf.has_redirect_from && !basic_auth {
        functions.host_redirect()
    } else {
        String::new()
    };
    extra.insert("host_redirect_function_code".into(), json!(host_redirect));
    let basic_auth_code = if basic_auth {
        functions.basic_auth()
    } else {
        String::new()
    };
    extra.insert("basic_auth_function_code".into(), json!(basic_auth_code));
    extra.insert("has_token_kvs".into(), json!(has_token_kvs));
    extra.insert(
        "origin_verify_secret".into(),
        json!(inputs.origin_verify_secret),
    );

    // secret の key 名は template に渡さない (bool だけ渡す)
    let mut context = dump(cf);
    if let Json::Object(map) = &mut context {
        for key in ["signing_key", "token_secret", "basic_auth"] {
            map.shift_remove(key);
        }
    }
    let vars = variables(&name, &export, extra, context);
    Ok(StackTemplate {
        body: render_template("cloudfront", &vars)?,
        name,
        region: cf.region.clone(),
        capabilities: Vec::new(),
        tags: Vec::new(),
        export,
    })
}

/// Python の `bool(value)` (None と空文字が偽)
fn is_set(value: &Option<String>) -> bool {
    value.as_deref().is_some_and(|v| !v.is_empty())
}

/// ACM stack (us-east-1) のメイン証明書の ARN。redirect_from のドメインは専用の
/// 証明書ではなくメインの SAN に載るので 1 本だけ
fn acm_certificate_arn(
    cf: &CloudFrontContext,
    inputs: &CloudFrontInputs,
) -> Result<Option<String>> {
    if !is_set(&cf.domain) {
        return Ok(None);
    }
    let Some(output) = &inputs.acm_output else {
        bail!(
            "ACM stack '{}' が見つかりません。先に ACM スタックをデプロイしてください。",
            acm_stack_name(cf)
        );
    };
    Ok(output.get("CertificateArn").cloned())
}

fn waf_acl_arn(cf: &CloudFrontContext, inputs: &CloudFrontInputs) -> Result<Option<String>> {
    if cf.waf.is_none() {
        return Ok(None);
    }
    let Some(output) = &inputs.waf_output else {
        bail!(
            "WAF stack '{}' が見つかりません。先に WAF スタックをデプロイしてください。",
            waf_stack_name(cf)
        );
    };
    Ok(output.get("WebACLArn").cloned())
}

/// CloudFront Function のコード生成
struct FunctionCodes<'a> {
    cf: &'a CloudFrontContext,
    /// 非 canonical host → canonical domain の 301 (redirect_from が無ければ空)
    host_redirect_prelude: String,
    /// Basic 認証 (basic_auth が無ければ空)
    basic_auth_prelude: String,
}

impl<'a> FunctionCodes<'a> {
    fn new(cf: &'a CloudFrontContext) -> Result<Self> {
        let host_redirect_prelude = if cf.has_redirect_from {
            let vars = json!({"canonical_domain": cf.domain});
            render_source("cf_function_host_redirect_prelude.js", &vars)?
                .trim_end_matches('\n')
                .to_string()
        } else {
            String::new()
        };
        let basic_auth_prelude = if is_set(&cf.basic_auth) {
            render_source("cf_function_basic_auth_prelude.js", &json!({}))?
                .trim_end_matches('\n')
                .to_string()
        } else {
            String::new()
        };
        Ok(Self {
            cf,
            host_redirect_prelude,
            basic_auth_prelude,
        })
    }

    /// 全 viewer-request Function 共通の prelude 合成。basic auth を先に差し込み、
    /// その後 host redirect を同じ位置に差し込むので、実行順は host redirect → basic auth
    fn inject_viewer_preludes(&self, code: &str) -> String {
        self.inject_host_redirect(&self.inject_basic_auth(code))
    }

    fn inject_host_redirect(&self, code: &str) -> String {
        if self.host_redirect_prelude.is_empty() {
            return code.to_string();
        }
        inject_after_request_line(code, &self.host_redirect_prelude)
    }

    /// KVS 参照のため handler を async にし、kvsHandle を持たない Function には
    /// 引数なし `cf.kvs()` の宣言を前置する
    fn inject_basic_auth(&self, code: &str) -> String {
        if self.basic_auth_prelude.is_empty() {
            return code.to_string();
        }
        let mut code = code.to_string();
        if !code.contains("kvsHandle") {
            code = format!("import cf from 'cloudfront';\nconst kvsHandle = cf.kvs();\n{code}");
        }
        if !code.contains("async function handler") {
            code = code.replacen("function handler(", "async function handler(", 1);
        }
        inject_after_request_line(&code, &self.basic_auth_prelude)
    }

    /// SPA の route ごとの viewer-request Function (yaml_key → コード)
    fn spa(&self) -> Result<Json> {
        let mut codes = Map::new();
        for route in self.cf.routes.iter().filter(|r| r.is_spa) {
            let fallback_uri = spa_fallback_uri(route);
            let code = if route.require_token {
                // KVS + HMAC 検証付き。Fn::Sub の 2 引数形式で `- |` の下は 12 スペース
                let vars = json!({"fallback_uri": fallback_uri, "login_path": route.login_path});
                let code = render_source("cf_function_spa_auth.js", &vars)?;
                reindent(&self.inject_viewer_preludes(&code), 12)
            } else {
                let vars = json!({"fallback_uri": fallback_uri});
                let code = render_source("cf_function_spa_fallback.js", &vars)?;
                reindent(&self.inject_viewer_preludes(&code), 8)
            };
            codes.insert(route.yaml_key.clone(), json!(code));
        }
        Ok(Json::Object(codes))
    }

    /// deploy_hash route の hash prefix を外す Function。
    ///
    /// route の prefix 直下の 1 要素が git hash の形なら、値を問わず外す。`DEPLOY_HASH`
    /// を git hash の形でない値で上書きしている場合だけ、その値も外す
    fn deploy_hash(&self) -> Result<Json> {
        let mut codes = Map::new();
        let deploy_hash = &self.cf.deploy_hash;
        if deploy_hash.is_empty() {
            return Ok(Json::Object(codes));
        }
        let mut segment_condition = format!("/^{DEPLOY_HASH_PATTERN}$/.test(segment)");
        if !is_git_hash(deploy_hash) {
            segment_condition.push_str(" || segment === ");
            segment_condition.push_str(&pyjson::dumps(&json!(deploy_hash)));
        }
        for route in self.cf.routes.iter().filter(|r| r.is_deploy_hash) {
            let vars = json!({
                "prefix": format!("{}/", route_prefix(route)),
                "hash_header": DEPLOY_HASH_HEADER,
                "segment_condition": segment_condition,
            });
            let code = render_source("cf_function_deploy_hash_strip.js", &vars)?;
            let code = reindent(&self.inject_viewer_preludes(&code), 8);
            codes.insert(route.yaml_key.clone(), json!(code));
        }
        Ok(Json::Object(codes))
    }

    /// versioning route の cache-control を付ける viewer-response Function
    fn cache_control(&self) -> Result<Json> {
        let mut codes = Map::new();
        for route in self.cf.routes.iter().filter(|r| r.versioning.is_some()) {
            let vars = json!({"max_age": route.versioned_max_age});
            let code = render_source("cf_function_cache_control.js", &vars)?;
            codes.insert(route.yaml_key.clone(), json!(reindent(&code, 8)));
        }
        Ok(Json::Object(codes))
    }

    /// API route 用の X-Forwarded-Host 付与 Function
    fn api_host(&self) -> Result<String> {
        let code = render_source("cf_function_api_host.js", &json!({}))?;
        Ok(reindent(&self.inject_viewer_preludes(&code), 8))
    }

    /// viewer-request Function を持たない behavior 用の単体 redirect Function
    fn host_redirect(&self) -> String {
        let code = format!(
            "function handler(event) {{\n    {REQUEST_LINE}\n{}\n    return request;\n}}",
            self.host_redirect_prelude
        );
        reindent(&code, 8)
    }

    /// viewer-request Function を持たない behavior 用の単体 Basic 認証 Function
    /// (redirect_from 有効時は host redirect prelude も同居する)
    fn basic_auth(&self) -> String {
        let code =
            format!("function handler(event) {{\n    {REQUEST_LINE}\n    return request;\n}}");
        reindent(&self.inject_viewer_preludes(&code), 8)
    }
}

/// `var request = event.request;` の直後に prelude を差し込む
fn inject_after_request_line(code: &str, prelude: &str) -> String {
    let mut out: Vec<&str> = Vec::new();
    let mut injected = false;
    for line in split_lines(code) {
        out.push(line);
        if !injected && line.contains(REQUEST_LINE) {
            out.push(prelude);
            injected = true;
        }
    }
    out.join("\n")
}

/// `FunctionCode: |` ブロック用に 2 行目以降を indent スペース字下げする
fn reindent(code: &str, indent: usize) -> String {
    let pad = " ".repeat(indent);
    split_lines(code)
        .into_iter()
        .enumerate()
        .map(|(i, line)| {
            if i == 0 {
                line.to_string()
            } else {
                format!("{pad}{line}")
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// path_pattern の glob を除いた prefix (`/admin/*` → `/admin`)
fn route_prefix(route: &RouteContext) -> &str {
    route
        .path_pattern
        .trim_end_matches('*')
        .trim_end_matches('/')
}

/// SPA fallback の書き換え先 URI (`/admin/*` → `/admin/index.html`)
fn spa_fallback_uri(route: &RouteContext) -> String {
    format!("{}/{}", route_prefix(route), route.spa_fallback_html)
}

/// `DEPLOY_HASH_PATTERN` (`[0-9a-f]{7,40}`) に全体が一致するか
fn is_git_hash(value: &str) -> bool {
    (7..=40).contains(&value.len())
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_is_git_hash() {
        assert!(is_git_hash("abc1234"));
        assert!(is_git_hash(&"a".repeat(40)));
        assert!(!is_git_hash("abc123"));
        assert!(!is_git_hash(&"a".repeat(41)));
        assert!(!is_git_hash("ABC1234"));
        assert!(!is_git_hash("v1.2.3-rc"));
    }

    #[test]
    fn test_inject_and_reindent() {
        let code =
            "function handler(event) {\n    var request = event.request;\n    return request;\n}";
        let injected = inject_after_request_line(code, "    X;");
        assert_eq!(
            injected,
            "function handler(event) {\n    var request = event.request;\n    X;\n    return request;\n}"
        );
        assert_eq!(reindent("a\n\nb", 2), "a\n  \n  b");
    }
}
