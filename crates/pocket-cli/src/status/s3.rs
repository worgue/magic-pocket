//! S3 bucket の状態 (Python 版 `resources/s3.py` の `status`)。
//!
//! 望む設定 (pocket.toml 由来) と今の設定を、boto3 が返す形の JSON にそろえて
//! 完全一致で比べる (Python の dict の `!=` と同じ。key の過不足も不一致)

use magic_pocket_settings::context::{CloudFrontContext, S3Context};
use magic_pocket_settings::types::OrderedMap;
use serde_json::{json, Map, Value as Json};

use super::sink::Sink;

/// 望む CORS ルール。cors の宣言が無い・origin が 1 つも無いときは `None`
pub fn desired_cors(
    s3: &S3Context,
    cloudfront: &OrderedMap<CloudFrontContext>,
    sink: &mut dyn Sink,
) -> Option<Json> {
    let cors = s3.cors.as_ref()?;
    let mut origins = Vec::new();
    for name in &cors.cloudfront_names {
        let Some(cf) = cloudfront.get(name) else {
            sink.warning(&format!("CORS: cloudfront '{name}' が見つかりません"));
            continue;
        };
        match cf.domain.as_deref().filter(|d| !d.is_empty()) {
            Some(domain) => origins.push(format!("https://{domain}")),
            None => origins.push("https://*.cloudfront.net".to_string()),
        }
    }
    if origins.is_empty() {
        return None;
    }
    Some(json!([{
        "AllowedOrigins": origins,
        "AllowedMethods": cors.methods,
        "AllowedHeaders": ["*"],
        "MaxAgeSeconds": 3600,
    }]))
}

/// 望む lifecycle ルール。宣言が無ければ `None`
pub fn desired_lifecycle(s3: &S3Context) -> Option<Json> {
    if s3.lifecycle_rules.is_empty() {
        return None;
    }
    let rules: Vec<Json> = s3
        .lifecycle_rules
        .iter()
        .map(|rule| {
            let mut item = Map::new();
            item.insert("ID".to_string(), json!(rule.id));
            item.insert("Status".to_string(), json!("Enabled"));
            item.insert("Filter".to_string(), json!({"Prefix": rule.prefix}));
            if let Some(days) = rule.expiration_days {
                item.insert("Expiration".to_string(), json!({"Days": days}));
            }
            if let Some(days) = rule.noncurrent_version_expiration_days {
                item.insert(
                    "NoncurrentVersionExpiration".to_string(),
                    json!({"NoncurrentDays": days}),
                );
            }
            Json::Object(item)
        })
        .collect();
    Some(Json::Array(rules))
}

/// versioning の設定が望みどおりか。`current` は `get_bucket_versioning` の Status
pub fn versioning_matches(s3: &S3Context, current: Option<&str>) -> bool {
    if s3.versioning {
        current == Some("Enabled")
    } else {
        current != Some("Enabled")
    }
}

/// 今の CORS ルール (SDK の型) を boto3 が返す形の JSON にする
pub fn cors_rules_json(rules: &[aws_sdk_s3::types::CorsRule]) -> Json {
    let list = |values: &[String]| Json::from(values.to_vec());
    let rules: Vec<Json> = rules
        .iter()
        .map(|rule| {
            let mut item = Map::new();
            if let Some(id) = rule.id.as_ref() {
                item.insert("ID".to_string(), json!(id));
            }
            if let Some(headers) = rule.allowed_headers.as_ref() {
                item.insert("AllowedHeaders".to_string(), list(headers));
            }
            item.insert("AllowedMethods".to_string(), list(&rule.allowed_methods));
            item.insert("AllowedOrigins".to_string(), list(&rule.allowed_origins));
            if let Some(headers) = rule.expose_headers.as_ref() {
                item.insert("ExposeHeaders".to_string(), list(headers));
            }
            if let Some(seconds) = rule.max_age_seconds {
                item.insert("MaxAgeSeconds".to_string(), json!(seconds));
            }
            Json::Object(item)
        })
        .collect();
    Json::Array(rules)
}

/// 今の lifecycle ルール (SDK の型) を boto3 が返す形の JSON にする。
///
/// pocket が書かない項目 (transition・タグの filter など) を持つルールは、望む形と
/// 必ず違うので `Other` の key を足して不一致にする
pub fn lifecycle_rules_json(rules: &[aws_sdk_s3::types::LifecycleRule]) -> Json {
    let rules: Vec<Json> = rules
        .iter()
        .map(|rule| {
            let mut item = Map::new();
            let mut other = false;
            if let Some(id) = rule.id.as_ref() {
                item.insert("ID".to_string(), json!(id));
            }
            item.insert("Status".to_string(), json!(rule.status.as_str()));
            if let Some(filter) = rule.filter.as_ref() {
                let mut value = Map::new();
                if let Some(prefix) = filter.prefix.as_ref() {
                    value.insert("Prefix".to_string(), json!(prefix));
                }
                other |= filter.tag.is_some()
                    || filter.and.is_some()
                    || filter.object_size_greater_than.is_some()
                    || filter.object_size_less_than.is_some();
                item.insert("Filter".to_string(), Json::Object(value));
            }
            if let Some(expiration) = rule.expiration.as_ref() {
                let mut value = Map::new();
                if let Some(days) = expiration.days {
                    value.insert("Days".to_string(), json!(days));
                }
                other |=
                    expiration.date.is_some() || expiration.expired_object_delete_marker.is_some();
                item.insert("Expiration".to_string(), Json::Object(value));
            }
            if let Some(noncurrent) = rule.noncurrent_version_expiration.as_ref() {
                let mut value = Map::new();
                if let Some(days) = noncurrent.noncurrent_days {
                    value.insert("NoncurrentDays".to_string(), json!(days));
                }
                other |= noncurrent.newer_noncurrent_versions.is_some();
                item.insert(
                    "NoncurrentVersionExpiration".to_string(),
                    Json::Object(value),
                );
            }
            #[allow(deprecated)]
            let deprecated_prefix = rule.prefix.is_some();
            other |= deprecated_prefix
                || rule.transitions.is_some()
                || rule.noncurrent_version_transitions.is_some()
                || rule.abort_incomplete_multipart_upload.is_some();
            if other {
                item.insert("Other".to_string(), json!(true));
            }
            Json::Object(item)
        })
        .collect();
    Json::Array(rules)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stack::testing::vector_context;
    use crate::status::sink::Recorded;
    use aws_sdk_s3::types::{
        CorsRule, ExpirationStatus, LifecycleExpiration, LifecycleRule, LifecycleRuleFilter,
        NoncurrentVersionExpiration, Transition,
    };
    use magic_pocket_settings::context::{S3CorsContext, S3LifecycleRuleContext};

    fn s3() -> S3Context {
        S3Context {
            region: "ap-northeast-1".to_string(),
            bucket_name: "b".to_string(),
            cors: None,
            versioning: false,
            lifecycle_rules: Vec::new(),
        }
    }

    #[test]
    fn test_versioning_matches_both_directions() {
        let mut ctx = s3();
        assert!(versioning_matches(&ctx, None));
        assert!(versioning_matches(&ctx, Some("Suspended")));
        assert!(!versioning_matches(&ctx, Some("Enabled")));
        ctx.versioning = true;
        assert!(versioning_matches(&ctx, Some("Enabled")));
        assert!(!versioning_matches(&ctx, None));
        assert!(!versioning_matches(&ctx, Some("Suspended")));
    }

    #[test]
    fn test_desired_lifecycle_matches_deployed_rule() {
        let mut ctx = s3();
        assert_eq!(desired_lifecycle(&ctx), None);
        ctx.lifecycle_rules.push(S3LifecycleRuleContext {
            id: "tmp".to_string(),
            prefix: "tmp/".to_string(),
            expiration_days: Some(7),
            noncurrent_version_expiration_days: Some(3),
        });
        let deployed = LifecycleRule::builder()
            .id("tmp")
            .status(ExpirationStatus::Enabled)
            .filter(LifecycleRuleFilter::builder().prefix("tmp/").build())
            .expiration(LifecycleExpiration::builder().days(7).build())
            .noncurrent_version_expiration(
                NoncurrentVersionExpiration::builder()
                    .noncurrent_days(3)
                    .build(),
            )
            .build()
            .unwrap();
        assert_eq!(
            desired_lifecycle(&ctx),
            Some(lifecycle_rules_json(std::slice::from_ref(&deployed)))
        );
        // 日数が違う・孤立ルールが残っている・pocket が書かない項目がある → 不一致
        let other_days = LifecycleRule::builder()
            .id("tmp")
            .status(ExpirationStatus::Enabled)
            .filter(LifecycleRuleFilter::builder().prefix("tmp/").build())
            .expiration(LifecycleExpiration::builder().days(8).build())
            .build()
            .unwrap();
        assert_ne!(
            desired_lifecycle(&ctx),
            Some(lifecycle_rules_json(std::slice::from_ref(&other_days)))
        );
        assert_ne!(
            desired_lifecycle(&ctx),
            Some(lifecycle_rules_json(&[deployed.clone(), other_days]))
        );
        let mut with_transition = deployed;
        with_transition.transitions = Some(vec![Transition::builder().days(30).build()]);
        assert_ne!(
            desired_lifecycle(&ctx),
            Some(lifecycle_rules_json(&[with_transition]))
        );
    }

    #[test]
    fn test_desired_cors_uses_cloudfront_domain() {
        let vector = vector_context("toml/cloudfront_object_hash.dev");
        let (name, cf) = vector.cloudfront.iter().next().unwrap();
        let mut ctx = s3();
        let mut sink = Recorded::default();
        assert_eq!(desired_cors(&ctx, &vector.cloudfront, &mut sink), None);
        ctx.cors = Some(S3CorsContext {
            methods: vec!["GET".to_string()],
            cloudfront_names: vec![name.clone(), "missing".to_string()],
        });
        let desired = desired_cors(&ctx, &vector.cloudfront, &mut sink).unwrap();
        let origin = format!("https://{}", cf.domain.as_deref().unwrap());
        assert_eq!(desired[0]["AllowedOrigins"], json!([origin]));
        assert_eq!(sink.0[0].2, "CORS: cloudfront 'missing' が見つかりません");
        let deployed = CorsRule::builder()
            .allowed_origins(origin)
            .allowed_methods("GET")
            .allowed_headers("*")
            .max_age_seconds(3600)
            .build()
            .unwrap();
        assert_eq!(desired, cors_rules_json(std::slice::from_ref(&deployed)));
        // AWS 側に ID や ExposeHeaders が付いていれば不一致
        let mut with_id = deployed;
        with_id.id = Some("x".to_string());
        assert_ne!(desired, cors_rules_json(&[with_id]));
    }
}
