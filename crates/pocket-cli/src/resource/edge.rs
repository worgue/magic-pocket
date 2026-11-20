//! `pocket-rs resource cloudfront` / `cloudfront-keys` / `cloudfront-waf` の読み取り
//! (Python 版 `cloudfront_cli.py` / `cloudfront_keys_cli.py` / `cloudfront_waf_cli.py` の
//! yaml・context・status)。

use anyhow::{bail, Result};
use magic_pocket_settings::context::{CloudFrontContext, Context};
use magic_pocket_settings::{mask_secret_values, pyjson};

use super::{load, CloudFrontCommand, EdgeCommand, EdgeTarget};
use crate::stack::ops::ResourceStatus;
use crate::stack::StackTemplate;
use crate::status::aws::AwsEvaluator;
use crate::status::report::Evaluator;
use crate::status::resource::Resource;
use crate::status::sink::Sink;

/// edge 系の resource の種類
#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    CloudFront,
    Keys,
    Waf,
}

impl Kind {
    /// 対象の cloudfront (Python の `get_cloudfront*_resources`)
    fn targets<'c>(
        self,
        context: &'c Context,
        name: Option<&str>,
    ) -> Result<Vec<&'c CloudFrontContext>> {
        if context.cloudfront.is_empty() {
            bail!("cloudfront is not configured for this stage");
        }
        let name = name.filter(|n| !n.is_empty());
        if self == Kind::CloudFront {
            return match name {
                Some(name) => match context.cloudfront.get(name) {
                    Some(cf) => Ok(vec![cf]),
                    None => bail!("cloudfront '{name}' is not configured"),
                },
                None => Ok(context.cloudfront.values().collect()),
            };
        }
        let results: Vec<&CloudFrontContext> = context
            .cloudfront
            .iter()
            .filter(|(_, cf)| match self {
                Kind::Keys => cf.signing_key.as_deref().is_some_and(|k| !k.is_empty()),
                _ => cf.waf.is_some(),
            })
            .filter(|(n, _)| name.is_none_or(|name| name == n.as_str()))
            .map(|(_, cf)| cf)
            .collect();
        if let (Some(name), true) = (name, results.is_empty()) {
            let label = if self == Kind::Keys {
                "cloudfront_keys"
            } else {
                "cloudfront_waf"
            };
            bail!("{label} '{name}' is not configured");
        }
        Ok(results)
    }

    fn resource(self, cf: &CloudFrontContext) -> Resource<'_> {
        match self {
            Kind::CloudFront => Resource::CloudFront(cf),
            Kind::Keys => Resource::CloudFrontKeys(cf),
            Kind::Waf => Resource::CloudFrontWaf(cf),
        }
    }

    async fn template(
        self,
        evaluator: &mut AwsEvaluator<'_>,
        cf: &CloudFrontContext,
        sink: &mut dyn Sink,
    ) -> Result<StackTemplate> {
        match self {
            Kind::CloudFront => evaluator.cloudfront_template(cf, sink).await,
            Kind::Keys => evaluator.keys_template(cf, sink).await,
            Kind::Waf => evaluator.waf_template(cf, sink).await,
        }
    }
}

pub async fn run_cloudfront(command: CloudFrontCommand, sink: &mut dyn Sink) -> Result<()> {
    match command {
        CloudFrontCommand::Yaml(target) => yaml(Kind::CloudFront, &target, sink).await,
        CloudFrontCommand::Status(target) => status(Kind::CloudFront, &target, sink).await,
        CloudFrontCommand::Context(target) => {
            let (context, _sdk) = load(&target.stage.stage).await?;
            for cf in Kind::CloudFront.targets(&context, target.name.as_deref())? {
                sink.info(&format!("[{}]", cf.name));
                let dump = mask_secret_values(&serde_json::to_value(cf)?);
                sink.print(&pyjson::dumps_indent2(&dump));
            }
            Ok(())
        }
    }
}

pub async fn run_keys(command: EdgeCommand, sink: &mut dyn Sink) -> Result<()> {
    edge(Kind::Keys, command, sink).await
}

pub async fn run_waf(command: EdgeCommand, sink: &mut dyn Sink) -> Result<()> {
    edge(Kind::Waf, command, sink).await
}

async fn edge(kind: Kind, command: EdgeCommand, sink: &mut dyn Sink) -> Result<()> {
    match command {
        EdgeCommand::Yaml(target) => yaml(kind, &target, sink).await,
        EdgeCommand::Status(target) => status(kind, &target, sink).await,
    }
}

async fn yaml(kind: Kind, target: &EdgeTarget, sink: &mut dyn Sink) -> Result<()> {
    let (context, sdk) = load(&target.stage.stage).await?;
    let targets = kind.targets(&context, target.name.as_deref())?;
    let mut evaluator = super::evaluator(&context, &sdk);
    // Python 版は表示の前に全部の secret を読む (secret の警告は先頭にまとまる)
    let mut templates = Vec::new();
    for cf in &targets {
        templates.push(kind.template(&mut evaluator, cf, sink).await?);
    }
    for (cf, template) in targets.iter().zip(templates) {
        sink.info(&format!("[{}]", cf.name));
        sink.print(&template.body);
    }
    Ok(())
}

async fn status(kind: Kind, target: &EdgeTarget, sink: &mut dyn Sink) -> Result<()> {
    let (context, sdk) = load(&target.stage.stage).await?;
    let targets = kind.targets(&context, target.name.as_deref())?;
    let mut evaluator = super::evaluator(&context, &sdk);
    let mut statuses = Vec::new();
    for cf in &targets {
        statuses.push(evaluator.status(&kind.resource(cf), sink).await?);
    }
    for (cf, status) in targets.iter().zip(statuses) {
        sink.info(&format!("[{}]", cf.name));
        print_status(status, sink);
    }
    Ok(())
}

/// COMPLETED は stderr (success)、それ以外は状態名を stdout に出す (Python 版と同じ)
fn print_status(status: ResourceStatus, sink: &mut dyn Sink) {
    if status == ResourceStatus::Completed {
        sink.success("COMPLETED");
    } else {
        sink.print(status.as_str());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stack::testing::vector_context;
    use crate::status::sink::{Recorded, Stream, Style};

    #[test]
    fn test_targets_filter_by_kind_and_name() {
        let ctx = vector_context("example/example-dsql.sandbox");
        let names = |kind: Kind, name: Option<&str>| -> Result<Vec<String>> {
            Ok(kind
                .targets(&ctx, name)?
                .iter()
                .map(|cf| cf.name.clone())
                .collect())
        };
        let all = names(Kind::CloudFront, None).unwrap();
        assert!(!all.is_empty());
        assert_eq!(
            names(Kind::CloudFront, Some(&all[0])).unwrap(),
            [all[0].clone()]
        );
        assert_eq!(
            names(Kind::CloudFront, Some("nope"))
                .unwrap_err()
                .to_string(),
            "cloudfront 'nope' is not configured"
        );
        // example-dsql は WAF を持たない
        assert!(names(Kind::Waf, None).unwrap().is_empty());
        assert_eq!(
            names(Kind::Waf, Some(&all[0])).unwrap_err().to_string(),
            format!("cloudfront_waf '{}' is not configured", all[0])
        );
    }

    #[test]
    fn test_status_output_stream() {
        let mut sink = Recorded::default();
        print_status(ResourceStatus::Completed, &mut sink);
        print_status(ResourceStatus::RequireUpdate, &mut sink);
        assert_eq!(
            sink.0,
            [
                (Stream::Stderr, Style::Success, "COMPLETED".to_string()),
                (Stream::Stdout, Style::Plain, "REQUIRE_UPDATE".to_string()),
            ]
        );
    }
}
