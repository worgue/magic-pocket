//! `pocket-rs destroy` の本体。手順の順と文言は [`super::destroy`]、ここは各手順を
//! AWS の部品につなぐ

use anyhow::{Context as _, Result};
use aws_config::SdkConfig;
use magic_pocket_settings::context::{Context, VpcContext};
use magic_pocket_settings::model::ProvisioningMode;

use super::backup::{AwsBackup, BackupApi as _, BackupDeploy};
use super::destroy::{
    self, ContainerFact, DestroyOps, DestroyOptions, Facts, RecoveryPoints, Step, VpcFact,
    NEON_CREDENTIAL_MISSING,
};
use super::edge::{EdgeDeploy, EdgeWaits};
use super::efs::{self, AwsEfs};
use super::inbound::AwsInbound;
use super::inbound_domain::AwsInboundDomain;
use super::interaction::Interaction;
use super::migrations::{self, Repositories as _};
use super::neon::Neon;
use super::parts;
use super::provider_urls::ProviderUrls;
use super::run::AwsRepositories;
use super::secret_store::AwsSecretStore;
use super::stacks::{AwsStacks, Stacks as _};
use super::state::StateStore;
use super::stored_value::AwsStoredValues;
use super::tidb::{MysqlSql, TiDb};
use super::upstash::Upstash;
use super::vpc::AwsVpcNetwork;
use super::{dsql, s3};
use crate::aws_util::{has_code, regional_config, sdk_error};
use crate::context::load_context;
use crate::image_build::codebuild::Names;
use crate::stack::cloudfront as cf_stack;
use crate::stack::ops::{cfn_status, ResourceStatus, StackDescription, WaitOptions};
use crate::stack::vpc as vpc_stack;
use crate::status::aws::AwsEvaluator;
use crate::status::providers::Http;
use crate::status::report::{Evaluated, Evaluator as _};
use crate::status::resource::Resource;
use crate::status::sink::{Sink, Terminal};

pub struct DestroyArgs {
    pub stage: String,
    pub without_secrets: bool,
    pub with_state_bucket: bool,
    pub yes: bool,
}

pub async fn run(args: DestroyArgs) -> Result<()> {
    let sdk = crate::aws_util::load_sdk().await;
    crate::image_build::check_aws_credentials(&sdk).await?;
    let (ctx, _) = load_context(&args.stage)?;
    let prompt = Interaction {
        assume_yes: args.yes,
    };
    let options = DestroyOptions {
        with_secrets: !args.without_secrets,
        with_state_bucket: args.with_state_bucket,
    };
    let mut ops = AwsDestroy {
        sdk,
        ctx,
        prompt: Interaction {
            assume_yes: args.yes,
        },
    };
    destroy::run(
        &mut ops,
        &args.stage,
        options,
        &prompt,
        &mut Terminal::new(),
    )
    .await
}

struct AwsDestroy {
    sdk: SdkConfig,
    ctx: Context,
    prompt: Interaction,
}

/// container が使う VPC (container 名の順。名前の重複を除く)
fn container_vpcs(ctx: &Context) -> Vec<&VpcContext> {
    let mut names: Vec<&String> = ctx.container.keys().collect();
    names.sort();
    let mut vpcs: Vec<&VpcContext> = Vec::new();
    for name in names {
        let vpc = ctx.container.get(name).and_then(|c| c.vpc.as_ref());
        if let Some(vpc) = vpc.filter(|vpc| !vpcs.iter().any(|known| known.name == vpc.name)) {
            vpcs.push(vpc);
        }
    }
    vpcs
}

/// この VPC を使っている project (stack の `pocket:consumer:<slug>` のタグ)
fn consumers(description: Option<&StackDescription>) -> Vec<String> {
    description
        .map(|d| d.tags.as_slice())
        .unwrap_or_default()
        .iter()
        .filter_map(|(key, _)| key.strip_prefix("pocket:consumer:"))
        .map(str::to_string)
        .collect()
}

/// 資格情報 (API key) が入っている Neon の設定
fn has_key(key: &Option<String>) -> bool {
    key.as_deref().is_some_and(|key| !key.is_empty())
}

impl AwsDestroy {
    fn codebuild_names(&self) -> Result<Names> {
        let prefix = migrations::resource_prefix(&self.ctx)?;
        Ok(Names::new(
            &prefix,
            self.ctx.external_roles,
            &self.ctx.external_role_prefix,
        ))
    }

    fn general_region(&self) -> Result<&str> {
        let general = self.ctx.general.as_ref();
        Ok(&general.context("general context is not configured")?.region)
    }

    async fn role_exists(&self, iam: &aws_sdk_iam::Client, name: &str) -> Result<bool> {
        match iam.get_role().role_name(name).send().await {
            Ok(_) => Ok(true),
            Err(e) if has_code(&e, &["NoSuchEntity", "NoSuchEntityException"]) => Ok(false),
            Err(e) => Err(sdk_error(&format!("IAM get_role ({name})"), &e)),
        }
    }

    /// CodeBuild の project か role (今の名前・旧名) が在るか。container が無ければ見ない
    async fn codebuild_exists(&self) -> Result<bool> {
        if self.ctx.container.is_empty() || self.ctx.general.is_none() {
            return Ok(false);
        }
        let names = self.codebuild_names()?;
        let config = regional_config(&self.sdk, self.general_region()?);
        let response = aws_sdk_codebuild::Client::new(&config)
            .batch_get_projects()
            .names(&names.project)
            .send()
            .await
            .map_err(|e| sdk_error("codebuild batch_get_projects", &e))?;
        if !response.projects().is_empty() {
            return Ok(true);
        }
        let iam = aws_sdk_iam::Client::new(&config);
        Ok(self.role_exists(&iam, &names.role).await?
            || self.role_exists(&iam, &names.legacy_role).await?)
    }

    /// リソースが在るか (状態が NOEXIST でないか)
    async fn exists(&self, resource: &Resource<'_>, sink: &mut dyn Sink) -> Result<bool> {
        let mut evaluator = AwsEvaluator::new(&self.ctx, self.sdk.clone());
        Ok(match evaluator.status(resource, sink).await? {
            Evaluated::Status(status) => status != ResourceStatus::NoExist,
            Evaluated::Skip(_) => false,
        })
    }

    async fn stack_exists(&self, region: &str, name: &str) -> Result<bool> {
        let stacks = AwsStacks { sdk: &self.sdk };
        let description = stacks.describe(region, name).await?;
        Ok(cfn_status(description.as_ref())? != ResourceStatus::NoExist)
    }

    async fn facts(&self, sink: &mut dyn Sink) -> Result<Facts> {
        let (sdk, ctx) = (&self.sdk, &self.ctx);
        let stacks = AwsStacks { sdk };
        let repos = AwsRepositories { sdk };
        let mut facts = Facts::default();
        let mut names: Vec<&String> = ctx.container.keys().collect();
        names.sort();
        for name in names {
            let c = ctx.container.get(name).expect("container");
            facts.containers.push(ContainerFact {
                name: name.clone(),
                ecr_exists: repos.exists(&c.region, &c.ecr_name).await?,
                ecr_name_overridden: c.ecr_name_overridden,
            });
        }
        if !ctx.container.is_empty() {
            facts.legacy_stack = migrations::legacy_container_stack_status(ctx, &stacks)
                .await?
                .is_some();
            facts.legacy_ecr = migrations::legacy_ecr_repo_exists(ctx, &repos).await?;
            facts.codebuild = self.codebuild_exists().await?;
        }
        if let Some(backup) = ctx.backup.as_ref() {
            let api = AwsBackup {
                sdk,
                region: &backup.region,
            };
            for plan in &backup.cleanup_plan_names {
                match api.find_plan_id(plan).await {
                    Ok(Some(_)) => facts.backup_plans.push(plan.clone()),
                    Ok(None) => {}
                    // 権限が無ければ「無し」として扱う (一覧の表示用)
                    Err(error) if is_access_denied(&error) => {
                        facts.backup_plans.clear();
                        break;
                    }
                    Err(error) => return Err(error),
                }
            }
        }
        if let Some(context) = ctx.dsql.as_ref() {
            facts.dsql = self.exists(&Resource::Dsql(context), sink).await?;
        }
        if let Some(rds) = ctx.rds.as_ref() {
            facts.rds = self.exists(&Resource::Rds(rds), sink).await?;
        }
        let http = Http::new()?;
        if let Some(tidb) = deploy_managed(ctx.tidb.as_ref(), |t| t.provisioning) {
            facts.tidb = TiDb::new(&http, tidb, &MysqlSql).cluster_exists().await?;
        }
        if let Some(upstash) = deploy_managed(ctx.upstash.as_ref(), |u| u.provisioning) {
            let upstash = Upstash {
                http: &http,
                ctx: upstash,
            };
            facts.upstash = upstash.database_exists().await?;
        }
        if let Some(neon) = deploy_managed(ctx.neon.as_ref(), |n| n.provisioning) {
            if has_key(&neon.api_key) {
                let neon = Neon {
                    http: &http,
                    ctx: neon,
                };
                facts.neon = neon.destroy_plan(sink).await?;
            } else {
                sink.warning(NEON_CREDENTIAL_MISSING);
            }
        }
        let efs_api = AwsEfs { sdk };
        for vpc in container_vpcs(ctx) {
            if !vpc.manage {
                facts.vpcs.push(VpcFact::default());
                continue;
            }
            let name = vpc_stack::stack_name(vpc);
            let description = stacks.describe(&vpc.region, &name).await?;
            let efs_exists = match vpc.efs.as_ref() {
                Some(context) => efs::status(&efs_api, context).await? != ResourceStatus::NoExist,
                None => false,
            };
            facts.vpcs.push(VpcFact {
                manage: true,
                stack_exists: cfn_status(description.as_ref())? != ResourceStatus::NoExist,
                consumers: consumers(description.as_ref()),
                efs_exists,
            });
        }
        if let Some(context) = ctx.s3.as_ref() {
            let client = aws_sdk_s3::Client::new(&regional_config(sdk, &context.region));
            facts.s3 = s3::bucket_exists(&client, &context.bucket_name).await?;
        }
        Ok(facts)
    }

    async fn delete_log_group(&self, region: &str, name: &str) -> Result<bool> {
        let client = aws_sdk_cloudwatchlogs::Client::new(&regional_config(&self.sdk, region));
        match client.delete_log_group().log_group_name(name).send().await {
            Ok(_) => Ok(true),
            Err(e) if has_code(&e, &["ResourceNotFoundException"]) => Ok(false),
            Err(e) => Err(sdk_error(&format!("logs delete_log_group ({name})"), &e)),
        }
    }

    async fn destroy_vpc(&self, sink: &mut dyn Sink) -> Result<()> {
        let sdk = &self.sdk;
        let stacks = AwsStacks { sdk };
        let efs_api = AwsEfs { sdk };
        for vpc in container_vpcs(&self.ctx) {
            let name = vpc_stack::stack_name(vpc);
            let description = stacks.describe(&vpc.region, &name).await?;
            let stack_exists = cfn_status(description.as_ref())? != ResourceStatus::NoExist;
            if !vpc.manage {
                // 外部の VPC: この project が使っている印 (タグ) だけを外す
                let arn = description.as_ref().map(|d| d.stack_id.as_str());
                if let Some(arn) = arn.filter(|arn| stack_exists && !arn.is_empty()) {
                    let slug = format!("{}-{}", self.ctx.stage, self.ctx.project_name);
                    let config = regional_config(sdk, &vpc.region);
                    aws_sdk_resourcegroupstagging::Client::new(&config)
                        .untag_resources()
                        .resource_arn_list(arn)
                        .tag_keys(format!("pocket:consumer:{slug}"))
                        .send()
                        .await
                        .map_err(|e| sdk_error("tagging untag_resources", &e))?;
                    sink.log("外部 VPC の consumer タグを削除しました。");
                }
                continue;
            }
            let users = consumers(description.as_ref());
            if !users.is_empty() {
                sink.danger("VPC に consumer がいるため削除できません:");
                for user in users {
                    sink.info(&format!("  - {user}"));
                }
                continue;
            }
            let efs_exists = match vpc.efs.as_ref() {
                Some(context) => efs::status(&efs_api, context).await? != ResourceStatus::NoExist,
                None => false,
            };
            if stack_exists || efs_exists {
                sink.log("Destroying VPC...");
                let network = AwsVpcNetwork { sdk };
                parts::vpc_deploy(&stacks, &efs_api, &network)
                    .delete(vpc)
                    .await?;
                sink.success("VPC was destroyed.");
            }
        }
        Ok(())
    }

    async fn destroy_backup(&self, sink: &mut dyn Sink) -> Result<()> {
        let Some(backup) = self.ctx.backup.as_ref() else {
            return Ok(());
        };
        let api = AwsBackup {
            sdk: &self.sdk,
            region: &backup.region,
        };
        BackupDeploy { api: &api }.delete(backup, sink).await?;
        let targets = parts::backup_targets(&self.sdk, &self.ctx).await?;
        let points = AwsRecoveryPoints {
            sdk: &self.sdk,
            region: &backup.region,
            vault_name: &backup.vault_name,
            resource_arns: targets.into_values().collect(),
        };
        let declared = !backup.plans.is_empty();
        destroy::destroy_recovery_points(&points, declared, backup.deletable, &self.prompt, sink)
            .await
    }
}

/// `provisioning = "deploy"` のときだけ設定を返す
fn deploy_managed<T>(
    settings: Option<&T>,
    provisioning: impl Fn(&T) -> ProvisioningMode,
) -> Option<&T> {
    settings.filter(|s| provisioning(s) == ProvisioningMode::Deploy)
}

fn is_access_denied(error: &anyhow::Error) -> bool {
    crate::aws_util::error_code(error) == Some("AccessDeniedException")
}

/// AWS Backup の recovery point (pocket が管理する vault の分だけ)
struct AwsRecoveryPoints<'a> {
    sdk: &'a SdkConfig,
    region: &'a str,
    vault_name: &'a str,
    /// 今ある cluster の ARN (消えた cluster の分は引けない)
    resource_arns: Vec<String>,
}

impl RecoveryPoints for AwsRecoveryPoints<'_> {
    async fn list(&self) -> Result<Option<Vec<String>>> {
        let client = aws_sdk_backup::Client::new(&regional_config(self.sdk, self.region));
        let mut arns = Vec::new();
        for resource_arn in &self.resource_arns {
            let mut pages = client
                .list_recovery_points_by_resource()
                .resource_arn(resource_arn)
                .into_paginator()
                .send();
            while let Some(page) = pages.next().await {
                let page = match page {
                    Ok(page) => page,
                    Err(e) if has_code(&e, &["AccessDeniedException"]) => return Ok(None),
                    Err(e) => return Err(sdk_error("backup list_recovery_points_by_resource", &e)),
                };
                // 利用者の vault に取った分は利用者の持ち物として扱い、消さない
                arns.extend(
                    page.recovery_points()
                        .iter()
                        .filter(|point| point.backup_vault_name() == Some(self.vault_name))
                        .filter_map(|point| point.recovery_point_arn().map(str::to_string)),
                );
            }
        }
        Ok(Some(arns))
    }

    async fn delete(&self, arn: &str) -> Result<()> {
        aws_sdk_backup::Client::new(&regional_config(self.sdk, self.region))
            .delete_recovery_point()
            .backup_vault_name(self.vault_name)
            .recovery_point_arn(arn)
            .send()
            .await
            .map_err(|e| sdk_error("backup delete_recovery_point", &e))?;
        Ok(())
    }
}

impl DestroyOps for AwsDestroy {
    async fn targets(
        &mut self,
        options: DestroyOptions,
        sink: &mut dyn Sink,
    ) -> Result<Vec<String>> {
        let facts = self.facts(sink).await?;
        Ok(destroy::targets(&self.ctx, &facts, options))
    }

    async fn step(&mut self, step: Step, sink: &mut dyn Sink) -> Result<()> {
        let (sdk, ctx) = (&self.sdk, &self.ctx);
        let stacks = AwsStacks { sdk };
        let repos = AwsRepositories { sdk };
        match step {
            Step::Inbound => {
                let aws = AwsInbound { sdk };
                let deploy = parts::inbound_deploy(&stacks, &aws);
                for inlet in ctx.inbound.values() {
                    deploy.delete(inlet).await?;
                }
                Ok(())
            }
            Step::InboundDomain => {
                let aws = AwsInboundDomain { sdk };
                let deploy = parts::inbound_domain_deploy(&stacks, &aws);
                for domain in ctx.inbound_domain.values() {
                    deploy.delete(domain).await?;
                }
                Ok(())
            }
            Step::CloudFrontAndAcm => {
                for (name, cf) in ctx.cloudfront.iter() {
                    let stack_name = cf_stack::cloudfront_stack_name(cf);
                    if self.stack_exists(&cf.region, &stack_name).await? {
                        sink.log(&format!("Destroying CloudFront '{name}'..."));
                        let (stacks, aws, store) = parts::cloudfront_env(sdk);
                        parts::cloudfront_deploy(&stacks, &aws, &store)
                            .delete(cf, sink)
                            .await?;
                        sink.success(&format!("CloudFront '{name}' was destroyed."));
                    }
                    if cf.domain.as_deref().is_none_or(str::is_empty) {
                        continue;
                    }
                    let acm = cf_stack::acm_stack_name(cf);
                    if self.stack_exists("us-east-1", &acm).await? {
                        sink.log(&format!("Destroying CloudFront ACM '{name}'..."));
                        let (store, urls) =
                            (AwsSecretStore::new(sdk.clone()), ProviderUrls::new()?);
                        let edge = EdgeDeploy {
                            stacks: &stacks,
                            store: &store,
                            urls: &urls,
                            waits: EdgeWaits::default(),
                        };
                        edge.acm_delete(cf, sink).await?;
                        sink.success(&format!("CloudFront ACM '{name}' was destroyed."));
                    }
                }
                Ok(())
            }
            Step::Containers => {
                let mut names: Vec<&String> = ctx.container.keys().collect();
                names.sort();
                let wait = destroy::container_delete_wait();
                for name in names {
                    let c = ctx.container.get(name).expect("container");
                    destroy::destroy_container(c, &stacks, &repos, wait, sink).await?;
                }
                Ok(())
            }
            Step::LegacyContainers => {
                if ctx.container.is_empty() {
                    return Ok(());
                }
                let wait = WaitOptions::new(1800, 10);
                destroy::destroy_legacy_containers(ctx, &stacks, &repos, wait, sink).await?;
                // 旧命名の Lambda ({prefix}{handler}) が作った log group
                let region = self.general_region()?;
                let prefix = migrations::resource_prefix(ctx)?;
                let mut keys: Vec<&String> = ctx
                    .container
                    .values()
                    .flat_map(|c| c.handlers.keys())
                    .collect();
                keys.sort();
                keys.dedup();
                for key in keys {
                    let name = format!("/aws/lambda/{prefix}{key}");
                    if self.delete_log_group(region, &name).await? {
                        sink.log(&format!("Deleted legacy log group: {name}"));
                    }
                }
                Ok(())
            }
            Step::CodeBuild => {
                if !self.codebuild_exists().await? {
                    return Ok(());
                }
                sink.log("Destroying CodeBuild resources...");
                let names = self.codebuild_names()?;
                let config = regional_config(sdk, self.general_region()?);
                let codebuild = aws_sdk_codebuild::Client::new(&config);
                match codebuild.delete_project().name(&names.project).send().await {
                    Ok(_) => {}
                    Err(e) if has_code(&e, &["ResourceNotFoundException"]) => {}
                    Err(e) => return Err(sdk_error("codebuild delete_project", &e)),
                }
                // external の role は利用者の持ち物なので消さない
                if !ctx.external_roles {
                    let iam = aws_sdk_iam::Client::new(&config);
                    super::iam::delete_role(&iam, &names.role, &[]).await?;
                    super::iam::delete_role(&iam, &names.legacy_role, &[]).await?;
                }
                sink.success("CodeBuild resources were deleted.");
                Ok(())
            }
            Step::LogGroups => {
                for c in ctx.container.values() {
                    for handler in c.handlers.values() {
                        let name = &handler.log_group_name;
                        if self.delete_log_group(&c.region, name).await? {
                            sink.log(&format!("Deleted log group: {name}"));
                        }
                    }
                }
                Ok(())
            }
            Step::Secrets => {
                if ctx.container.is_empty() {
                    return Ok(());
                }
                let mut names: Vec<&String> = ctx.container.keys().collect();
                names.sort();
                let views: Vec<_> = std::iter::once(ctx.secrets.as_ref())
                    .chain(
                        names
                            .iter()
                            .map(|name| ctx.container.get(name).and_then(|c| c.secrets.as_ref())),
                    )
                    .flatten()
                    .collect();
                if views.is_empty() {
                    return Ok(());
                }
                sink.log("Destroying pocket managed secrets...");
                let store = AwsSecretStore::new(sdk.clone());
                for view in views {
                    store.delete_all(view).await?;
                }
                sink.success("Pocket managed secrets were deleted.");
                Ok(())
            }
            Step::Backup => self.destroy_backup(sink).await,
            Step::Dsql => {
                let Some(context) = ctx.dsql.as_ref() else {
                    return Ok(());
                };
                if !self.exists(&Resource::Dsql(context), sink).await? {
                    return Ok(());
                }
                sink.log("Destroying DSQL cluster...");
                let (api, values) = (dsql::AwsDsql { sdk }, AwsStoredValues { sdk });
                parts::dsql_deploy(&api, &values)
                    .delete(context, sink)
                    .await
            }
            Step::Rds => {
                let Some(rds) = ctx.rds.as_ref() else {
                    return Ok(());
                };
                if !self.exists(&Resource::Rds(rds), sink).await? {
                    return Ok(());
                }
                sink.log("Destroying RDS Aurora cluster...");
                let (aws, stacks, values) = parts::rds_env(sdk, &rds.region);
                parts::rds_deploy(&aws, &stacks, &values)
                    .delete(rds, sink)
                    .await?;
                sink.success("RDS Aurora cluster was destroyed. Final snapshot was created.");
                Ok(())
            }
            Step::Vpc => self.destroy_vpc(sink).await,
            Step::CloudFrontKeys => {
                for (name, cf) in ctx.cloudfront.iter() {
                    if cf.signing_key.as_deref().is_none_or(str::is_empty) {
                        continue;
                    }
                    let stack_name = cf_stack::keys_stack_name(cf);
                    if !self.stack_exists(&cf.region, &stack_name).await? {
                        continue;
                    }
                    sink.log(&format!("Destroying CloudFrontKeys '{name}'..."));
                    let (store, urls) = (AwsSecretStore::new(sdk.clone()), ProviderUrls::new()?);
                    let edge = EdgeDeploy {
                        stacks: &stacks,
                        store: &store,
                        urls: &urls,
                        waits: EdgeWaits::default(),
                    };
                    edge.keys_delete(cf, sink).await?;
                    sink.success(&format!("CloudFrontKeys '{name}' was destroyed."));
                }
                Ok(())
            }
            Step::S3 => {
                let Some(context) = ctx.s3.as_ref() else {
                    return Ok(());
                };
                let client = aws_sdk_s3::Client::new(&regional_config(sdk, &context.region));
                if !s3::bucket_exists(&client, &context.bucket_name).await? {
                    return Ok(());
                }
                sink.log("Destroying S3 bucket...");
                s3::delete_bucket_with_contents(&client, &context.bucket_name).await?;
                sink.success("S3 bucket was deleted.");
                Ok(())
            }
            Step::Tidb => {
                let provisioning = ctx.tidb.as_ref().map(|tidb| tidb.provisioning);
                let hint = "pocket resource tidb delete";
                match destroy::provider_action(provisioning, "TiDB", hint) {
                    None => Ok(()),
                    Some(Err(warning)) => {
                        sink.warning(&warning);
                        Ok(())
                    }
                    Some(Ok(())) => {
                        let http = Http::new()?;
                        let tidb = ctx.tidb.as_ref().expect("tidb");
                        let tidb = TiDb::new(&http, tidb, &MysqlSql);
                        if tidb.cluster_exists().await? {
                            sink.log("Destroying TiDB cluster...");
                            tidb.delete_cluster().await?;
                            sink.success("TiDB cluster was deleted.");
                        }
                        Ok(())
                    }
                }
            }
            Step::Upstash => {
                let provisioning = ctx.upstash.as_ref().map(|upstash| upstash.provisioning);
                match destroy::provider_action(provisioning, "Upstash", "Upstash コンソール等")
                {
                    None => Ok(()),
                    Some(Err(warning)) => {
                        sink.warning(&warning);
                        Ok(())
                    }
                    Some(Ok(())) => {
                        let http = Http::new()?;
                        let upstash = Upstash {
                            http: &http,
                            ctx: ctx.upstash.as_ref().expect("upstash"),
                        };
                        if upstash.database_exists().await? {
                            upstash.delete_database(sink).await?;
                        }
                        Ok(())
                    }
                }
            }
            Step::Neon => {
                let provisioning = ctx.neon.as_ref().map(|neon| neon.provisioning);
                let hint = "pocket resource neon delete";
                match destroy::provider_action(provisioning, "Neon", hint) {
                    None => Ok(()),
                    Some(Err(warning)) => {
                        sink.warning(&warning);
                        Ok(())
                    }
                    Some(Ok(())) => {
                        let neon = ctx.neon.as_ref().expect("neon");
                        if !has_key(&neon.api_key) {
                            sink.warning(NEON_CREDENTIAL_MISSING);
                            return Ok(());
                        }
                        let http = Http::new()?;
                        let neon = Neon {
                            http: &http,
                            ctx: neon,
                        };
                        neon.destroy(sink).await
                    }
                }
            }
            Step::StateBucket => {
                sink.log("Destroying state bucket...");
                StateStore::new(sdk, ctx)?.delete_bucket().await?;
                sink.success("State bucket was deleted.");
                Ok(())
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::deploy::stacks::fake::FakeStacks;
    use crate::stack::testing::vector_context;

    #[test]
    fn test_consumers_come_from_stack_tags() {
        let mut description = FakeStacks::description("CREATE_COMPLETE", "h", &[]);
        description.tags.push((
            "pocket:consumer:dev-other".to_string(),
            "deployed".to_string(),
        ));
        description
            .tags
            .push(("pocket:sharable".to_string(), "true".to_string()));
        assert_eq!(consumers(Some(&description)), ["dev-other"]);
        assert!(consumers(None).is_empty());
    }

    #[test]
    fn test_deploy_managed_filters_command_provisioning() {
        let deploy = Some(&ProvisioningMode::Deploy);
        let command = Some(&ProvisioningMode::Command);
        assert!(deploy_managed(deploy, |mode| *mode).is_some());
        assert!(deploy_managed(command, |mode| *mode).is_none());
        assert!(deploy_managed(None::<&ProvisioningMode>, |mode| *mode).is_none());
    }

    #[test]
    fn test_container_vpcs_are_unique_by_name() {
        let ctx = vector_context("example/example-dsql.sandbox");
        let vpcs = container_vpcs(&ctx);
        let mut names: Vec<&str> = vpcs.iter().map(|vpc| vpc.name.as_str()).collect();
        let before = names.len();
        names.dedup();
        assert_eq!(names.len(), before);
    }
}
