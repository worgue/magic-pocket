//! VPC の stack と、付属する EFS の手順 (Python 版 `resources/vpc.py`)。
//!
//! stack の作成・更新は完了を待たない (VPC を使う側が待つ)。削除は、stack の外から
//! VPC に付いているもの (他 project の ENI・security group) が残っていれば止める。

use std::collections::BTreeSet;

use anyhow::{bail, Result};
use magic_pocket_settings::context::VpcContext;

use super::efs::{self, EfsApi};
use super::stacks::{self, Stacks};
use crate::ec2::VpcContents;
use crate::stack::ops::{
    cfn_status, status_detail, template_synced, ResourceStatus, StackResource, WaitOptions,
};
use crate::stack::vpc as vpc_stack;

/// VPC の中身を引く (EC2)
pub trait VpcNetwork {
    async fn contents(&self, region: &str, vpc_ids: &[String]) -> Result<VpcContents>;
}

#[derive(Debug, Clone, Copy)]
pub struct VpcWaits {
    pub efs: WaitOptions,
    /// stack の削除待ち (NAT gateway の削除に時間がかかる)
    pub delete: WaitOptions,
}

impl Default for VpcWaits {
    fn default() -> Self {
        Self {
            efs: efs::default_wait(),
            delete: WaitOptions::new(1800, 10),
        }
    }
}

pub struct VpcDeploy<'a, K, E, N> {
    pub stacks: &'a K,
    pub efs: &'a E,
    pub network: &'a N,
    pub waits: VpcWaits,
}

/// stack の外から VPC に付いている ENI と security group (削除を妨げるもの)。
///
/// - `resources`: stack の resource (削除済みを除く)
/// - `owned_enis`: stack の NAT gateway / EFS mount target の ENI
pub fn deletion_dependencies(
    resources: &[StackResource],
    owned_enis: &BTreeSet<String>,
    contents: &VpcContents,
) -> Vec<String> {
    let ids: BTreeSet<&str> = resources
        .iter()
        .filter_map(|resource| resource.physical_id.as_deref())
        .filter(|id| !id.is_empty())
        .collect();
    let mut dependencies = Vec::new();
    for eni in &contents.network_interfaces {
        if ids.contains(eni.id.as_str()) || owned_enis.contains(&eni.id) {
            continue;
        }
        dependencies.push(format!(
            "ENI {} type={} status={} subnet={} description={}",
            eni.id,
            eni.interface_type.as_deref().unwrap_or("-"),
            eni.status.as_deref().unwrap_or("-"),
            eni.subnet_id.as_deref().unwrap_or("-"),
            eni.description.as_deref().unwrap_or_default(),
        ));
    }
    for group in &contents.security_groups {
        if ids.contains(group.id.as_str()) || group.name == "default" {
            continue;
        }
        dependencies.push(format!(
            "SG {} name={} description={}",
            group.id,
            group.name,
            group.description.as_deref().unwrap_or_default(),
        ));
    }
    dependencies
}

impl<K: Stacks, E: EfsApi, N: VpcNetwork> VpcDeploy<'_, K, E, N> {
    /// stack の前に EFS を用意する (template が file system の id を参照する)
    pub async fn deploy_init(&self, vpc: &VpcContext) -> Result<()> {
        match vpc.efs.as_ref() {
            Some(efs) => efs::ensure_exists(self.efs, efs, self.waits.efs).await,
            None => Ok(()),
        }
    }

    async fn template(&self, vpc: &VpcContext) -> Result<crate::stack::StackTemplate> {
        let filesystem_id = match vpc.efs.as_ref() {
            Some(efs) => self
                .efs
                .find(&efs.region, &efs.name)
                .await?
                .map(|found| found.id),
            None => None,
        };
        vpc_stack::vpc_stack(vpc, filesystem_id.as_deref())
    }

    pub async fn create(&self, vpc: &VpcContext) -> Result<()> {
        println!("Creating cloudformation stack for vpc ...");
        self.stacks.create(&self.template(vpc).await?).await
    }

    /// template が変わっていれば更新する
    pub async fn update(&self, vpc: &VpcContext) -> Result<()> {
        let stack = self.template(vpc).await?;
        let description = self.stacks.describe(&stack.region, &stack.name).await?;
        if template_synced(description.as_ref(), &stack)? {
            return Ok(());
        }
        self.stacks.update(&stack).await
    }

    /// stack を消し、EFS が在れば消す
    pub async fn delete(&self, vpc: &VpcContext) -> Result<()> {
        let name = vpc_stack::stack_name(vpc);
        let description = self.stacks.describe(&vpc.region, &name).await?;
        if cfn_status(description.as_ref())? != ResourceStatus::NoExist {
            // 削除が進行中なら、依存の調査と削除の指示は済んでいる
            if status_detail(description.as_ref()) != "DELETE_IN_PROGRESS" {
                let dependencies = self.stack_dependencies(vpc, &name).await?;
                if !dependencies.is_empty() {
                    bail!(
                        "VPC の削除を妨げるスタック外の依存があります:\n{}\n\
                         所有元の VPC 離脱・削除と ENI 解放を確認して再実行してください。\
                         available の ENI や pocket 命名の SG でも、名前や状態だけでは\
                         削除可能と判断できません。所有元・他の利用者を確認してください。",
                        dependencies.join("\n")
                    );
                }
                self.stacks.delete(&vpc.region, &name).await?;
            }
            stacks::wait(
                self.stacks,
                &vpc.region,
                &name,
                "vpc",
                ResourceStatus::NoExist,
                self.waits.delete,
            )
            .await?;
        }
        if let Some(efs) = vpc.efs.as_ref() {
            efs::delete_if_exists(self.efs, efs).await?;
        }
        Ok(())
    }

    async fn stack_dependencies(&self, vpc: &VpcContext, name: &str) -> Result<Vec<String>> {
        let resources: Vec<StackResource> = self
            .stacks
            .resources(&vpc.region, name)
            .await?
            .into_iter()
            .filter(|resource| resource.status != "DELETE_COMPLETE")
            .collect();
        let physical =
            |resource: &StackResource| resource.physical_id.clone().filter(|id| !id.is_empty());
        let vpc_ids: Vec<String> = resources
            .iter()
            .filter(|resource| resource.resource_type == "AWS::EC2::VPC")
            .filter_map(physical)
            .collect();
        if vpc_ids.is_empty() {
            return Ok(Vec::new());
        }
        let contents = self.network.contents(&vpc.region, &vpc_ids).await?;
        let ids: BTreeSet<String> = resources.iter().filter_map(physical).collect();
        let mut owned = BTreeSet::new();
        for nat in &contents.nat_gateways {
            if ids.contains(&nat.id) {
                owned.extend(nat.network_interface_ids.iter().cloned());
            }
        }
        for resource in &resources {
            if resource.resource_type != "AWS::EFS::MountTarget" {
                continue;
            }
            let Some(id) = physical(resource) else {
                continue;
            };
            let eni = self
                .efs
                .mount_target_network_interface(&vpc.region, &id)
                .await?;
            owned.extend(eni);
        }
        Ok(deletion_dependencies(&resources, &owned, &contents))
    }
}

/// AWS に対する [`VpcNetwork`]
pub struct AwsVpcNetwork<'a> {
    pub sdk: &'a aws_config::SdkConfig,
}

impl VpcNetwork for AwsVpcNetwork<'_> {
    async fn contents(&self, region: &str, vpc_ids: &[String]) -> Result<VpcContents> {
        let config = crate::aws_util::regional_config(self.sdk, region);
        crate::ec2::vpc_contents(&config, vpc_ids).await
    }
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;

    use serde_json::json;

    use super::*;
    use crate::deploy::efs::fake::FakeEfs;
    use crate::deploy::poll::testing::{fast, run};
    use crate::deploy::stacks::fake::FakeStacks;
    use crate::ec2::{NatGateway, NetworkInterface, SecurityGroup};
    use crate::stack::testing::vector_context;

    #[derive(Default)]
    struct FakeNetwork {
        contents: VpcContents,
        calls: RefCell<Vec<String>>,
    }

    impl VpcNetwork for FakeNetwork {
        async fn contents(&self, _region: &str, vpc_ids: &[String]) -> Result<VpcContents> {
            self.calls.borrow_mut().push(vpc_ids.join(","));
            Ok(self.contents.clone())
        }
    }

    fn waits() -> VpcWaits {
        VpcWaits {
            efs: fast(3),
            delete: fast(3),
        }
    }

    /// EFS 付きの managed VPC
    fn vpc() -> VpcContext {
        let ctx = vector_context("toml/default.dev");
        let found = ctx
            .container
            .values()
            .find_map(|c| c.vpc.clone().filter(|vpc| vpc.efs.is_some()));
        found.expect("EFS 付きの VPC")
    }

    fn resource(kind: &str, id: &str) -> StackResource {
        StackResource {
            logical_id: id.to_string(),
            resource_type: kind.to_string(),
            physical_id: Some(id.to_string()),
            status: "CREATE_COMPLETE".to_string(),
            status_reason: None,
        }
    }

    fn eni(id: &str) -> NetworkInterface {
        NetworkInterface {
            id: id.to_string(),
            interface_type: Some("interface".to_string()),
            status: Some("in-use".to_string()),
            subnet_id: Some("subnet-1".to_string()),
            description: Some("lambda".to_string()),
        }
    }

    fn group(id: &str, name: &str) -> SecurityGroup {
        SecurityGroup {
            id: id.to_string(),
            name: name.to_string(),
            description: Some("desc".to_string()),
        }
    }

    #[test]
    fn test_deletion_dependencies_ignore_stack_owned_resources() {
        let resources = [
            resource("AWS::EC2::VPC", "vpc-1"),
            resource("AWS::EC2::SecurityGroup", "sg-stack"),
        ];
        let contents = VpcContents {
            network_interfaces: vec![eni("eni-nat"), eni("eni-other")],
            security_groups: vec![
                group("sg-stack", "stack"),
                group("sg-default", "default"),
                group("sg-other", "prod-other-rds"),
            ],
            nat_gateways: Vec::new(),
        };
        let owned = BTreeSet::from(["eni-nat".to_string()]);
        assert_eq!(
            deletion_dependencies(&resources, &owned, &contents),
            [
                "ENI eni-other type=interface status=in-use subnet=subnet-1 description=lambda",
                "SG sg-other name=prod-other-rds description=desc",
            ]
        );
        let bare = NetworkInterface {
            id: "eni-bare".to_string(),
            ..Default::default()
        };
        let contents = VpcContents {
            network_interfaces: vec![bare],
            ..Default::default()
        };
        assert_eq!(
            deletion_dependencies(&resources, &BTreeSet::new(), &contents),
            ["ENI eni-bare type=- status=- subnet=- description="]
        );
    }

    #[test]
    fn test_init_creates_efs_and_create_does_not_wait() {
        let vpc = vpc();
        let (stacks, efs_api, network) = (
            FakeStacks::default(),
            FakeEfs::default(),
            FakeNetwork::default(),
        );
        *stacks.after_write.borrow_mut() = Some("CREATE_IN_PROGRESS".to_string());
        let deploy = VpcDeploy {
            stacks: &stacks,
            efs: &efs_api,
            network: &network,
            waits: waits(),
        };
        run(deploy.deploy_init(&vpc)).expect("init");
        assert_eq!(
            efs_api.calls()[0],
            format!("create {}", vpc.efs.as_ref().unwrap().name)
        );
        run(deploy.create(&vpc)).expect("create");
        let name = vpc_stack::stack_name(&vpc);
        // 進行中のまま返る (完了は VPC を使う側が待つ)
        assert_eq!(stacks.calls(), [format!("create {} {name}", vpc.region)]);
        // 作った stack は EFS の id を入れた template
        let want = vpc_stack::vpc_stack(&vpc, Some("fs-new")).expect("stack");
        let description = run(stacks.describe(&vpc.region, &name)).unwrap();
        assert!(template_synced(description.as_ref(), &want).unwrap());
    }

    #[test]
    fn test_update_only_when_template_changes() {
        let vpc = vpc();
        let efs_api = FakeEfs::with(&vpc.efs.as_ref().unwrap().name, "available", json!([]));
        let (stacks, network) = (FakeStacks::default(), FakeNetwork::default());
        let deploy = VpcDeploy {
            stacks: &stacks,
            efs: &efs_api,
            network: &network,
            waits: waits(),
        };
        let stack = vpc_stack::vpc_stack(&vpc, Some("fs-fake")).expect("stack");
        stacks.put_synced(&stack, &[]);
        run(deploy.update(&vpc)).expect("同じ");
        assert!(stacks.writes().is_empty());
        stacks.put(
            &stack.name,
            FakeStacks::description("UPDATE_COMPLETE", "0000000000000000", &[]),
        );
        run(deploy.update(&vpc)).expect("更新");
        assert_eq!(
            stacks.writes(),
            [format!("update {} {}", vpc.region, stack.name)]
        );
    }

    #[test]
    fn test_delete_checks_dependencies_then_stack_then_efs() {
        let vpc = vpc();
        let name = vpc_stack::stack_name(&vpc);
        let efs_name = vpc.efs.as_ref().unwrap().name.clone();

        // stack の外の ENI が残っていれば、何も消さずに止める
        let stacks = FakeStacks::default();
        stacks.put(&name, FakeStacks::description("CREATE_COMPLETE", "x", &[]));
        *stacks.resources.borrow_mut() = vec![
            resource("AWS::EC2::VPC", "vpc-1"),
            resource("AWS::EC2::NatGateway", "nat-1"),
            resource("AWS::EFS::MountTarget", "fsmt-1"),
        ];
        let mut efs_api = FakeEfs::with(&efs_name, "available", json!([]));
        efs_api.mount_targets = vec![("fsmt-1".to_string(), "eni-efs".to_string())];
        let mut network = FakeNetwork {
            contents: VpcContents {
                network_interfaces: vec![eni("eni-nat"), eni("eni-efs"), eni("eni-lambda")],
                security_groups: vec![group("sg-default", "default")],
                nat_gateways: vec![NatGateway {
                    id: "nat-1".to_string(),
                    network_interface_ids: vec!["eni-nat".to_string()],
                }],
            },
            ..Default::default()
        };
        let deploy = VpcDeploy {
            stacks: &stacks,
            efs: &efs_api,
            network: &network,
            waits: waits(),
        };
        let error = run(deploy.delete(&vpc)).expect_err("依存あり");
        assert!(error.to_string().contains("ENI eni-lambda"), "{error}");
        assert!(!error.to_string().contains("eni-nat"));
        assert!(!error.to_string().contains("eni-efs"));
        assert!(stacks.writes().is_empty() && efs_api.calls().is_empty());
        assert_eq!(network.calls.borrow().as_slice(), ["vpc-1"]);

        // 依存が無ければ stack → EFS の順に消す
        network.contents.network_interfaces.pop();
        let deploy = VpcDeploy {
            stacks: &stacks,
            efs: &efs_api,
            network: &network,
            waits: waits(),
        };
        run(deploy.delete(&vpc)).expect("delete");
        assert_eq!(stacks.writes(), [format!("delete {} {name}", vpc.region)]);
        assert_eq!(efs_api.calls(), ["delete fs-fake"]);
    }

    #[test]
    fn test_delete_in_progress_only_waits() {
        let vpc = vpc();
        let name = vpc_stack::stack_name(&vpc);
        let stacks = FakeStacks::default();
        stacks.put(
            &name,
            FakeStacks::description("DELETE_IN_PROGRESS", "x", &[]),
        );
        let (efs_api, network) = (FakeEfs::default(), FakeNetwork::default());
        let deploy = VpcDeploy {
            stacks: &stacks,
            efs: &efs_api,
            network: &network,
            waits: waits(),
        };
        // 削除中のままなら、依存の調査も削除の指示もせずに待って timeout する
        let error = run(deploy.delete(&vpc)).expect_err("timeout");
        assert!(error.to_string().contains("タイムアウト"), "{error}");
        assert!(stacks.writes().is_empty());
        assert!(network.calls.borrow().is_empty());
        assert!(
            efs_api.calls().is_empty(),
            "stack が消えるまで EFS は消さない"
        );
    }

    #[test]
    fn test_delete_without_stack_still_removes_efs() {
        let vpc = vpc();
        let efs_api = FakeEfs::with(&vpc.efs.as_ref().unwrap().name, "available", json!([]));
        let (stacks, network) = (FakeStacks::default(), FakeNetwork::default());
        let deploy = VpcDeploy {
            stacks: &stacks,
            efs: &efs_api,
            network: &network,
            waits: waits(),
        };
        run(deploy.delete(&vpc)).expect("delete");
        assert!(stacks.writes().is_empty());
        assert_eq!(efs_api.calls(), ["delete fs-fake"]);
    }

    #[test]
    fn test_default_delete_wait_matches_python() {
        let waits = VpcWaits::default();
        assert_eq!(
            (waits.delete.attempts, waits.delete.interval.as_secs()),
            (180, 10)
        );
        assert_eq!((waits.efs.attempts, waits.efs.interval.as_secs()), (100, 3));
    }
}
