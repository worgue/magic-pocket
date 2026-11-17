//! template vector (`tests/data/template_vectors`) との照合。
//!
//! 入力の toml から Context を組み、vector に同梱された外部入力 (`stack_external`) を
//! 渡して全 stack を描画する。template のテキスト・stack 名・region・capabilities・
//! tags・Export 名・template hash が Python 版と一致することを確かめる。

use std::collections::BTreeMap;
use std::path::Path;

use magic_pocket_settings::context::Context;
use serde_json::Value as Json;

use super::cloudfront::{self, CloudFrontInputs};
use super::container::{self, ContainerInputs, RdsState};
use super::testing::{context, read_json, read_text, repo_root, vector_dirs};
use super::{inbound, vpc, StackOutputs, StackTemplate};

fn string_map(value: &Json) -> BTreeMap<String, String> {
    value
        .as_object()
        .map(|map| {
            map.iter()
                .map(|(k, v)| (k.clone(), v.as_str().expect("文字列").to_string()))
                .collect()
        })
        .unwrap_or_default()
}

fn stack_output(external: &Json, name: &str) -> Option<StackOutputs> {
    let output = &external["stack_outputs"][name];
    output.is_object().then(|| string_map(output))
}

fn opt_string(value: &Json) -> Option<String> {
    value.as_str().map(str::to_string)
}

/// deploy が作る stack を Python 版の generator と同じ順 (種別, stack) で組む
fn build_stacks(ctx: &Context, external: &Json) -> Vec<(&'static str, StackTemplate)> {
    let secrets = &external["secrets"];
    let mut stacks = Vec::new();
    for cf in ctx.cloudfront.values() {
        if cf.domain.as_deref().is_some_and(|d| !d.is_empty()) {
            stacks.push(("acm", cloudfront::acm_stack(cf).expect("acm")));
        }
    }
    for cf in ctx.cloudfront.values() {
        if cf.waf.is_some() {
            let values = string_map(&secrets["waf_allow_secret_values"][&cf.name]);
            stacks.push(("waf", cloudfront::waf_stack(cf, &values).expect("waf")));
        }
    }
    for domain in ctx.inbound_domain.values() {
        let zone = external["inbound_domain_hosted_zone_ids"][&domain.stack_name].as_str();
        stacks.push((
            "inbound_domain",
            inbound::inbound_domain_stack(domain, zone),
        ));
    }
    let mut vpc_names: Vec<&str> = Vec::new();
    for c in ctx.container.values() {
        let Some(v) = c.vpc.as_ref().filter(|v| v.manage) else {
            continue;
        };
        if vpc_names.contains(&v.name.as_str()) {
            continue;
        }
        vpc_names.push(&v.name);
        let efs = external["efs_filesystem_id"].as_str();
        stacks.push(("vpc", vpc::vpc_stack(v, efs).expect("vpc")));
    }
    for cf in ctx.cloudfront.values() {
        if cf.signing_key.as_deref().is_some_and(|k| !k.is_empty()) {
            let pem = secrets["signing_public_key_pem"][&cf.name]
                .as_str()
                .unwrap_or_default();
            stacks.push((
                "cloudfront_keys",
                cloudfront::keys_stack(cf, pem).expect("keys"),
            ));
        }
    }
    let mut names: Vec<&String> = ctx.container.keys().collect();
    names.sort();
    for name in names {
        let c = ctx.container.get(name).expect("container");
        let rds = &external["rds"];
        let inputs = ContainerInputs {
            rds: RdsState {
                security_group_id: opt_string(&rds["security_group_id"]),
                endpoint: opt_string(&rds["cluster"]["Endpoint"]),
                port: rds["cluster"]["Port"].as_i64(),
                master_user_secret_arn: opt_string(
                    &rds["cluster"]["MasterUserSecret"]["SecretArn"],
                ),
                static_secret_arn: opt_string(&rds["static_secret_arn"]),
            },
            dsql_identifier: opt_string(&external["dsql_identifier"]),
            vpc_output: c
                .vpc
                .as_ref()
                .and_then(|v| stack_output(external, &vpc::stack_name(v))),
        };
        let stack = container::container_stack(
            c,
            ctx.rds.as_ref(),
            ctx.dsql.as_ref(),
            ctx.scheduler.get(name),
            &inputs,
        )
        .expect("container");
        stacks.push(("container", stack));
    }
    for inlet in ctx.inbound.values() {
        let binding = &external["inbound_bindings"][&inlet.resource_name];
        let rule_set = binding["rule_set"].as_str().expect("rule_set");
        let stack = inbound::inbound_stack(inlet, rule_set, binding["after"].as_str());
        stacks.push(("inbound", stack));
    }
    for cf in ctx.cloudfront.values() {
        let inputs = CloudFrontInputs {
            acm_output: stack_output(external, &cloudfront::acm_stack_name(cf)),
            waf_output: stack_output(external, &cloudfront::waf_stack_name(cf)),
            origin_verify_secret: secrets["origin_verify_secret"][&cf.name]
                .as_str()
                .unwrap_or_default()
                .to_string(),
        };
        stacks.push((
            "cloudfront",
            cloudfront::cloudfront_stack(cf, &inputs).expect("cloudfront"),
        ));
    }
    stacks
}

/// 1 件の stack を manifest の要素と比べ、違いを返す
fn compare(dir: &Path, want: &Json, got: &StackTemplate) -> Vec<String> {
    let mut problems = Vec::new();
    let file = dir.join(want["file"].as_str().expect("file"));
    let want_body = read_text(&file);
    if got.body.clone() + "\n" != want_body {
        problems.push(format!(
            "template が違います: {}",
            first_difference(&got.body, &want_body)
        ));
    }
    let tags: Vec<Json> = got
        .tags
        .iter()
        .map(|(k, v)| serde_json::json!({"Key": k, "Value": v}))
        .collect();
    for (label, got_value, want_value) in [
        ("region", Json::from(got.region.clone()), &want["region"]),
        (
            "capabilities",
            Json::from(got.capabilities.clone()),
            &want["capabilities"],
        ),
        ("tags", Json::from(tags), &want["tags"]),
        ("export", Json::Object(got.export.clone()), &want["export"]),
        (
            "template_hash",
            Json::from(got.template_hash()),
            &want["template_hash"],
        ),
    ] {
        if &got_value != want_value {
            problems.push(format!(
                "{label} が違います (rust {got_value} / python {want_value})"
            ));
        }
    }
    problems
}

fn first_difference(got: &str, want: &str) -> String {
    for (i, (g, w)) in got.lines().zip(want.lines()).enumerate() {
        if g != w {
            return format!("{} 行目\n    rust:   {g:?}\n    python: {w:?}", i + 1);
        }
    }
    format!(
        "行数が違います (rust {} / python {})",
        got.lines().count(),
        want.lines().count()
    )
}

#[test]
fn test_stacks_match_python() {
    let root = repo_root();
    let mut failures = Vec::new();
    let mut count = 0;
    for dir in vector_dirs() {
        let manifest = read_json(&dir.join("manifest.json"));
        let ctx = context(&root, &manifest);
        let stacks = build_stacks(&ctx, &manifest["stack_external"]);
        let wanted: Vec<&Json> = manifest["stacks"]
            .as_array()
            .expect("stacks")
            .iter()
            .collect();
        let got_names: Vec<(&str, &str)> =
            stacks.iter().map(|(k, s)| (*k, s.name.as_str())).collect();
        let want_names: Vec<(&str, &str)> = wanted
            .iter()
            .map(|s| (s["kind"].as_str().unwrap(), s["name"].as_str().unwrap()))
            .collect();
        if got_names != want_names {
            failures.push(format!(
                "{}: stack の並びが違います\n  rust:   {got_names:?}\n  python: {want_names:?}",
                dir.display()
            ));
            continue;
        }
        for (want, (_, got)) in wanted.iter().zip(&stacks) {
            count += 1;
            for problem in compare(&dir, want, got) {
                failures.push(format!("{} {}: {problem}", dir.display(), got.name));
            }
        }
    }
    assert!(
        failures.is_empty(),
        "{} 件の違い ({count} stack 中):\n{}",
        failures.len(),
        failures.join("\n")
    );
    assert!(count > 100, "vector が少なすぎます: {count}");
}
