//! `pocket-rs resource inbound --stage … --name … <command>` の読み取り (Python 版
//! `inbound_cli.py` の status / yaml / reconcile)。

use std::collections::BTreeSet;

use anyhow::{bail, Result};
use clap::Args;
use magic_pocket_rs::inbound::{receipt_id, SETUP_NOTIFICATION};
use magic_pocket_settings::context::{InboundContext, InboundDomainContext};
use magic_pocket_settings::pyjson;
use serde_json::{Map, Value as Json};

use super::inbound_ops::{inbound_outputs, AwsInboundOps};
use super::{load, InboundCommand};
use crate::aws_util::sdk_error;
use crate::deploy::inbound::AwsInbound;
use crate::deploy::inbound_domain::{dns_records, AwsInboundDomain, InboundDomainAws};
use crate::deploy::parts::inbound_deploy;
use crate::deploy::stacks::AwsStacks;
use crate::stack::ops::{stack_outputs, StackOutput};
use crate::status::report::Evaluator;
use crate::status::resource::Resource;
use crate::status::sink::Sink;

#[derive(Args, Clone)]
pub struct InboundTarget {
    /// stage 名
    #[arg(long, env = "POCKET_DEPLOY_STAGE")]
    pub stage: String,
    /// inboundの設定名
    #[arg(long)]
    pub name: String,
}

pub async fn run(
    target: &InboundTarget,
    command: InboundCommand,
    sink: &mut dyn Sink,
) -> Result<()> {
    if let InboundCommand::Destroy { yes } = command {
        return super::write::inbound_destroy(&target.stage, &target.name, yes, sink).await;
    }
    let (context, sdk) = load(&target.stage).await?;
    let Some(inlet) = context.inbound.get(&target.name) else {
        bail!("inbound.{} がありません", target.name);
    };
    match command {
        InboundCommand::Copy {
            identifier,
            to_stage,
            to_name,
        } => {
            return super::inbound_ops::run_copy(
                &sdk,
                inlet,
                &identifier,
                &to_stage,
                &to_name,
                sink,
            )
            .await;
        }
        InboundCommand::Import {
            manifest_key,
            handler_ref,
        } => {
            let ops = AwsInboundOps::new(&sdk, &inlet.region).await;
            return super::inbound_ops::import(
                &ops,
                &context,
                inlet,
                &manifest_key,
                &handler_ref,
                sink,
            )
            .await;
        }
        InboundCommand::Redrive { queue, limit } => {
            let outputs = inbound_outputs(&sdk, inlet).await?;
            let ops = AwsInboundOps::new(&sdk, &inlet.region).await;
            return super::inbound_ops::redrive(&ops, inlet, outputs.as_ref(), queue, limit, sink)
                .await;
        }
        _ => {}
    }
    let mut evaluator = super::evaluator(&context, &sdk);
    match command {
        InboundCommand::Yaml => {
            sink.print(&evaluator.inbound_template(inlet).await?.body);
            Ok(())
        }
        InboundCommand::Status => {
            let (stacks, aws) = (AwsStacks { sdk: &sdk }, AwsInbound { sdk: &sdk });
            inbound_deploy(&stacks, &aws).prepare_deploy(inlet).await?;
            let status = evaluator.status(&Resource::Inbound(inlet), sink).await?;
            sink.print(&format!("stack: {} / {status}", inlet.resource_name));
            let description = evaluator
                .describe(&inlet.region, &inlet.resource_name)
                .await?;
            let outputs = description.as_ref().map(|d| d.outputs.as_slice());
            sink.print(&pyjson::dumps_indent2_unicode(&output_json(
                outputs.unwrap_or_default(),
            )));
            let Some(domain) = context.inbound_domain.get(&inlet.config.domain) else {
                bail!("inbound domain {} がありません", inlet.config.domain);
            };
            let domain_status = evaluator
                .status(&Resource::InboundDomain(domain), sink)
                .await?;
            let verification = AwsInboundDomain { sdk: &sdk }
                .verification_status(&domain.region, &domain.domain)
                .await?;
            sink.print(&format!(
                "domain: {} / stack={} ({domain_status}) / verification={}",
                domain.domain,
                domain.stack_name,
                verification.as_deref().unwrap_or("None")
            ));
            if !domain.manage_dns {
                sink.print("manage_dns = false: 次のレコードをDNSに登録してください");
                let description = evaluator
                    .describe(&domain.region, &domain.stack_name)
                    .await?;
                for line in dns_lines(domain, description.as_ref().map(|d| d.outputs.as_slice()))? {
                    sink.print(&line);
                }
            }
            if inlet.config.delivery_alert.eventbridge {
                sink.print(
                    "EventBridge: source=aws.cloudwatch, detail-type=CloudWatch Alarm State Change",
                );
            }
            Ok(())
        }
        InboundCommand::Reconcile => {
            let description = evaluator
                .describe(&inlet.region, &inlet.resource_name)
                .await?;
            let outputs = stack_outputs(description.as_ref()).filter(|o| !o.is_empty());
            let Some(bucket) = outputs.as_ref().and_then(|o| o.get("Bucket")).cloned() else {
                bail!("受信スタックがありません");
            };
            let s3 = crate::s3_client(&inlet.region).await;
            let metadata: BTreeSet<String> = list_keys(&s3, &bucket, &inlet.config.metadata_prefix)
                .await?
                .into_iter()
                .collect();
            let raw = list_keys(&s3, &bucket, &inlet.config.raw_prefix).await?;
            let missing = missing_receipts(inlet, &bucket, &raw, &metadata);
            for (key, identifier) in &missing {
                sink.print(&format!("受信情報なし: {key} / {identifier}"));
            }
            if !missing.is_empty() {
                bail!(
                    "{}件の受信情報がありません。通知の遅延・DLQを確認してください",
                    missing.len()
                );
            }
            sink.print("原本に対応する受信情報を確認しました。");
            Ok(())
        }
        InboundCommand::Destroy { .. }
        | InboundCommand::Copy { .. }
        | InboundCommand::Import { .. }
        | InboundCommand::Redrive { .. } => unreachable!("先頭で処理する"),
    }
}

/// stack の Outputs を Python の `Stack.output` と同じ並びの dict にする
/// (OutputKey の後に ExportName。同じ key は後の値で上書き)
fn output_json(outputs: &[StackOutput]) -> Json {
    let mut map = Map::new();
    for output in outputs {
        map.insert(output.key.clone(), Json::from(output.value.clone()));
        if let Some(export) = output.export_name.as_ref() {
            map.insert(export.clone(), Json::from(output.value.clone()));
        }
    }
    Json::Object(map)
}

/// 登録が要る DNS record の行。stack が無ければ空 (Python の `print_dns_records`)
fn dns_lines(
    domain: &InboundDomainContext,
    outputs: Option<&[StackOutput]>,
) -> Result<Vec<String>> {
    let Some(outputs) = outputs.filter(|o| !o.is_empty()) else {
        return Ok(Vec::new());
    };
    let mut map = crate::stack::StackOutputs::new();
    for output in outputs {
        map.insert(output.key.clone(), output.value.clone());
        if let Some(export) = output.export_name.as_ref() {
            map.insert(export.clone(), output.value.clone());
        }
    }
    Ok(dns_records(domain, &map)?
        .into_iter()
        .map(|(kind, name, value)| format!("  {kind} {name} {value}"))
        .collect())
}

async fn list_keys(s3: &aws_sdk_s3::Client, bucket: &str, prefix: &str) -> Result<Vec<String>> {
    let mut keys = Vec::new();
    let mut pages = s3
        .list_objects_v2()
        .bucket(bucket)
        .prefix(prefix)
        .into_paginator()
        .send();
    while let Some(page) = pages.next().await {
        let page = page.map_err(|e| sdk_error("s3 list_objects_v2", &e))?;
        keys.extend(
            page.contents()
                .iter()
                .filter_map(|o| o.key().map(str::to_string)),
        );
    }
    Ok(keys)
}

/// 受信情報が無い原本の (key, 受信 ID)。SES の初期設定通知は除く
fn missing_receipts(
    inlet: &InboundContext,
    bucket: &str,
    raw_keys: &[String],
    metadata: &BTreeSet<String>,
) -> Vec<(String, String)> {
    let config = &inlet.config;
    let setup = format!("{}{SETUP_NOTIFICATION}", config.raw_prefix);
    raw_keys
        .iter()
        .filter(|key| **key != setup)
        .filter_map(|key| {
            let identifier = receipt_id(bucket, key);
            let expected = format!("{}{identifier}.json", config.metadata_prefix);
            (!metadata.contains(&expected)).then(|| (key.clone(), identifier))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stack::testing::vector_context;

    fn output(key: &str, value: &str, export: Option<&str>) -> StackOutput {
        StackOutput {
            key: key.to_string(),
            value: value.to_string(),
            export_name: export.map(str::to_string),
        }
    }

    #[test]
    fn test_output_json_keeps_python_order() {
        let outputs = [
            output("Bucket", "b", Some("x-bucket")),
            output("QueueUrl", "q", None),
        ];
        assert_eq!(
            pyjson::dumps_indent2_unicode(&output_json(&outputs)),
            "{\n  \"Bucket\": \"b\",\n  \"x-bucket\": \"b\",\n  \"QueueUrl\": \"q\"\n}"
        );
        assert_eq!(pyjson::dumps_indent2_unicode(&output_json(&[])), "{}");
    }

    #[test]
    fn test_missing_receipts_skips_setup_notification() {
        let ctx = vector_context("example/example-dsql.sandbox");
        let inlet = ctx.inbound.values().next().expect("inbound");
        let raw = inlet.config.raw_prefix.clone();
        let meta = inlet.config.metadata_prefix.clone();
        let keys = vec![
            format!("{raw}{SETUP_NOTIFICATION}"),
            format!("{raw}a"),
            format!("{raw}b"),
        ];
        let mut metadata = BTreeSet::new();
        metadata.insert(format!("{meta}{}.json", receipt_id("bk", &keys[1])));
        let missing = missing_receipts(inlet, "bk", &keys, &metadata);
        assert_eq!(missing, [(keys[2].clone(), receipt_id("bk", &keys[2]))]);
    }

    #[test]
    fn test_dns_lines_empty_without_stack() {
        let ctx = vector_context("example/example-dsql.sandbox");
        let domain = ctx.inbound_domain.values().next().expect("domain");
        assert!(dns_lines(domain, None).unwrap().is_empty());
    }
}
