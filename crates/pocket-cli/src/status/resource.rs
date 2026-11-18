//! status が見る resource の並び (Python 版 `deploy_cli.get_resources`)。

use magic_pocket_settings::context::{
    BackupContext, CloudFrontContext, ContainerContext, Context, DsqlContext, InboundContext,
    InboundDomainContext, RdsContext, S3Context, VpcContext,
};
use magic_pocket_settings::model::StoreType;
use serde_json::Value as Json;

use crate::stack;

/// deploy が管理する resource。名前は Python 版の class 名 (表示に使う)
pub enum Resource<'a> {
    CloudFrontAcm(&'a CloudFrontContext),
    CloudFrontWaf(&'a CloudFrontContext),
    InboundDomain(&'a InboundDomainContext),
    S3(&'a S3Context),
    Vpc(&'a VpcContext),
    Dsql(&'a DsqlContext),
    Rds(&'a RdsContext),
    Backup(&'a BackupContext),
    CloudFrontKeys(&'a CloudFrontContext),
    Container(&'a ContainerContext),
    Inbound(&'a InboundContext),
    CloudFront(&'a CloudFrontContext),
}

fn non_empty(value: &Option<String>) -> bool {
    value.as_deref().is_some_and(|v| !v.is_empty())
}

/// deploy と同じ順に resource を並べる
pub fn resources(ctx: &Context) -> Vec<Resource<'_>> {
    let mut list = Vec::new();
    // ACM 証明書と WAF は us-east-1。CloudFront stack より前
    for cf in ctx.cloudfront.values() {
        if non_empty(&cf.domain) {
            list.push(Resource::CloudFrontAcm(cf));
        }
    }
    for cf in ctx.cloudfront.values() {
        if cf.waf.is_some() {
            list.push(Resource::CloudFrontWaf(cf));
        }
    }
    list.extend(ctx.inbound_domain.values().map(Resource::InboundDomain));
    // Neon / TiDB は deploy が管理しない (接続 URL は user secret で渡す)
    if let Some(s3) = ctx.s3.as_ref() {
        list.push(Resource::S3(s3));
    }
    // 同じ VPC を複数の container が使っていても 1 回だけ
    let mut vpc_names: Vec<&str> = Vec::new();
    for container in ctx.container.values() {
        if let Some(vpc) = container.vpc.as_ref().filter(|v| v.manage) {
            if !vpc_names.contains(&vpc.name.as_str()) {
                vpc_names.push(&vpc.name);
                list.push(Resource::Vpc(vpc));
            }
        }
    }
    if let Some(dsql) = ctx.dsql.as_ref() {
        list.push(Resource::Dsql(dsql));
    }
    if let Some(rds) = ctx.rds.as_ref() {
        list.push(Resource::Rds(rds));
    }
    if let Some(backup) = ctx.backup.as_ref() {
        if !backup.plans.is_empty() || !backup.legacy_plan_names.is_empty() {
            list.push(Resource::Backup(backup));
        }
    }
    for cf in ctx.cloudfront.values() {
        if non_empty(&cf.signing_key) {
            list.push(Resource::CloudFrontKeys(cf));
        }
    }
    let mut names: Vec<&String> = ctx.container.keys().collect();
    names.sort();
    for name in names {
        list.push(Resource::Container(
            ctx.container.get(name).expect("container"),
        ));
    }
    list.extend(ctx.inbound.values().map(Resource::Inbound));
    list.extend(ctx.cloudfront.values().map(Resource::CloudFront));
    list
}

fn store_name(store: StoreType) -> &'static str {
    match store {
        StoreType::Sm => "sm",
        StoreType::Ssm => "ssm",
    }
}

fn to_json(value: &impl serde::Serialize) -> Json {
    serde_json::to_value(value).expect("context は JSON にできる")
}

impl Resource<'_> {
    pub fn class_name(&self) -> &'static str {
        match self {
            Resource::CloudFrontAcm(_) => "CloudFrontAcm",
            Resource::CloudFrontWaf(_) => "CloudFrontWaf",
            Resource::InboundDomain(_) => "InboundDomain",
            Resource::S3(_) => "S3",
            Resource::Vpc(_) => "Vpc",
            Resource::Dsql(_) => "Dsql",
            Resource::Rds(_) => "Rds",
            Resource::Backup(_) => "Backup",
            Resource::CloudFrontKeys(_) => "CloudFrontKeys",
            Resource::Container(_) => "Container",
            Resource::Inbound(_) => "Inbound",
            Resource::CloudFront(_) => "CloudFront",
        }
    }

    /// `--show-info` の説明 (Python 版の `description`。無い resource は `None`)
    pub fn description(&self) -> Option<String> {
        Some(match self {
            Resource::CloudFrontAcm(cf) => format!(
                "Create ACM certificates in us-east-1 for: {}",
                cf.domain.as_deref().unwrap_or("None")
            ),
            Resource::CloudFrontWaf(cf) => {
                format!("Create WAFv2 IPSet + WebACL in us-east-1 for: {}", cf.name)
            }
            Resource::InboundDomain(_) | Resource::Inbound(_) => return None,
            Resource::S3(s3) => format!("Create bucket: {}", s3.bucket_name),
            Resource::Vpc(vpc) => {
                let mut text = format!(
                    "Create aws cloudformation stack: {}",
                    stack::vpc::stack_name(vpc)
                );
                if let Some(efs) = vpc.efs.as_ref() {
                    text.push_str(&format!("\nCreate efs: {}", efs.name));
                }
                text
            }
            Resource::Dsql(dsql) => format!("Create Aurora DSQL cluster: {}", dsql.tag_name),
            Resource::Rds(rds) if !rds.managed => format!(
                "Reference existing RDS (secret_arn: {})",
                rds.secret_arn.as_deref().unwrap_or("None")
            ),
            Resource::Rds(rds) => format!(
                "Create Aurora PostgreSQL Serverless v2 cluster: {}",
                rds.cluster_identifier
            ),
            Resource::Backup(backup) if backup.plans.is_empty() => {
                "Clean up legacy AWS Backup plans".to_string()
            }
            Resource::Backup(backup) => format!(
                "Configure AWS Backup plans: {}",
                backup
                    .plans
                    .iter()
                    .map(|p| p.plan_name.as_str())
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
            Resource::CloudFrontKeys(cf) => format!(
                "Create CloudFront signing key resources for: {}",
                cf.signing_key.as_deref().unwrap_or("None")
            ),
            Resource::Container(container) => {
                let mut text = format!(
                    "Create aws cloudformation stack: {}\nCreate ecr repository: {}",
                    stack::container::stack_name(container),
                    container.ecr_name
                );
                for view in container.secrets_views() {
                    if !view.managed.is_empty() {
                        text.push_str(&format!(
                            "\nCreate pocket managed secrets ({}): {}",
                            store_name(view.store),
                            view.pocket_key
                        ));
                    }
                }
                text
            }
            Resource::CloudFront(cf) => format!(
                "Create cloudformation(for cloudfront) using s3 bucket: {}",
                cf.bucket_name
            ),
        })
    }

    /// `--show-info` で出す resource の context (Python の `model_dump(mode="json")`)
    pub fn context_json(&self) -> Json {
        match self {
            Resource::CloudFrontAcm(cf)
            | Resource::CloudFrontWaf(cf)
            | Resource::CloudFrontKeys(cf)
            | Resource::CloudFront(cf) => to_json(cf),
            Resource::InboundDomain(domain) => to_json(domain),
            Resource::S3(s3) => to_json(s3),
            Resource::Vpc(vpc) => to_json(vpc),
            Resource::Dsql(dsql) => to_json(dsql),
            Resource::Rds(rds) => to_json(rds),
            Resource::Backup(backup) => to_json(backup),
            Resource::Container(container) => to_json(container),
            Resource::Inbound(inbound) => to_json(inbound),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stack::testing::vector_context;

    fn names(vector: &str) -> Vec<&'static str> {
        let ctx = vector_context(vector);
        resources(&ctx).iter().map(Resource::class_name).collect()
    }

    #[test]
    fn test_resource_order_matches_deploy() {
        // example-dsql: 受信ドメイン・DSQL・Backup・inbound・CloudFront を持つ
        assert_eq!(
            names("example/example-dsql.sandbox"),
            [
                "CloudFrontAcm",
                "InboundDomain",
                "S3",
                "Dsql",
                "Backup",
                "Container",
                "Inbound",
                "CloudFront"
            ]
        );
    }

    #[test]
    fn test_provider_databases_are_not_listed() {
        let listed = names("toml/db_command_provisioning.dev");
        for name in ["Neon", "TiDb"] {
            assert!(!listed.contains(&name), "{name} は deploy の管理外");
        }
    }

    #[test]
    fn test_descriptions() {
        let ctx = vector_context("toml/rds.dev");
        let list = resources(&ctx);
        let find = |name: &str| {
            list.iter()
                .find(|r| r.class_name() == name)
                .unwrap_or_else(|| panic!("{name} がありません"))
                .description()
        };
        let vpc = find("Vpc").unwrap();
        assert!(
            vpc.starts_with("Create aws cloudformation stack: "),
            "{vpc}"
        );
        let rds = find("Rds").unwrap();
        assert!(
            rds.starts_with("Create Aurora PostgreSQL Serverless v2 cluster: "),
            "{rds}"
        );
        let container = find("Container").unwrap();
        assert!(
            container.contains("\nCreate ecr repository: "),
            "{container}"
        );
    }
}
