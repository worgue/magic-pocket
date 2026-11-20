//! `pocket-rs resource vpc` の読み取り (Python 版 `vpc_cli.py` の yaml / status)。

use anyhow::{bail, Result};
use magic_pocket_rs::config::find_toml_path;
use magic_pocket_settings::context::VpcContext;
use magic_pocket_settings::Settings;

use super::{VpcCommand, VpcTarget};
use crate::stack::ops::{cfn_status, stack_status, CfnClient, ResourceStatus, StackApi};
use crate::stack::vpc::stack_name;
use crate::status::aws::vpc_template;
use crate::status::sink::Sink;

pub async fn run(command: VpcCommand, sink: &mut dyn Sink) -> Result<()> {
    match command {
        VpcCommand::Yaml(target) => {
            let vpc = vpc_context(&target)?;
            let sdk = crate::aws_util::load_sdk().await;
            sink.print(&vpc_template(&sdk, &vpc).await?.body);
            Ok(())
        }
        VpcCommand::Status(target) => {
            let vpc = vpc_context(&target)?;
            let sdk = crate::aws_util::load_sdk().await;
            let description = CfnClient::new(&sdk, &vpc.region)
                .describe(&stack_name(&vpc))
                .await?;
            let mut status = cfn_status(description.as_ref())?;
            if status == ResourceStatus::Completed {
                let template = vpc_template(&sdk, &vpc).await?;
                status = stack_status(description.as_ref(), &template)?;
            }
            print_status(status, sink);
            Ok(())
        }
    }
}

/// `--stage` があればその stage の `[vpc]`、無ければ共通の `[vpc]` (Python の
/// `get_vpc_resource`。AWS は見ない)
fn vpc_context(target: &VpcTarget) -> Result<VpcContext> {
    let toml_path = find_toml_path()?;
    let Some(stage) = target.stage.as_deref() else {
        return match Settings::load_common_vpc(&toml_path)? {
            Some(vpc) => Ok(vpc),
            None => bail!("[vpc] が定義されていません"),
        };
    };
    let (settings, advisories) = Settings::load(&toml_path, stage)?;
    for advisory in advisories {
        eprintln!("{}", advisory.message);
    }
    match settings.vpc_context() {
        Some(vpc) => Ok(vpc),
        None => bail!(
            "stage '{stage}' に VPC が定義されていません。\
             撤去する場合は元の VPC 宣言を復元してください。"
        ),
    }
}

fn print_status(status: ResourceStatus, sink: &mut dyn Sink) {
    match status {
        ResourceStatus::Completed => sink.success("Vpc has been created."),
        ResourceStatus::NoExist => sink.warning("Vpc has not created yet."),
        ResourceStatus::Failed => sink.danger("Vpc has failed. Please check console."),
        other => sink.warning(&format!("Vpc stack status: {other}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::status::sink::{Recorded, Stream, Style};

    #[test]
    fn test_status_messages() {
        let mut sink = Recorded::default();
        for status in [
            ResourceStatus::Completed,
            ResourceStatus::NoExist,
            ResourceStatus::Failed,
            ResourceStatus::RequireUpdate,
        ] {
            print_status(status, &mut sink);
        }
        assert_eq!(
            sink.0,
            [
                (
                    Stream::Stderr,
                    Style::Success,
                    "Vpc has been created.".to_string()
                ),
                (
                    Stream::Stderr,
                    Style::Warning,
                    "Vpc has not created yet.".to_string()
                ),
                (
                    Stream::Stderr,
                    Style::Danger,
                    "Vpc has failed. Please check console.".to_string()
                ),
                (
                    Stream::Stderr,
                    Style::Warning,
                    "Vpc stack status: REQUIRE_UPDATE".to_string()
                ),
            ]
        );
    }
}
