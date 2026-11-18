//! deploy の部品を 1 つずつ実行する非表示サブコマンド (`pocket-rs deploy-parts`)。
//!
//! `pocket-rs deploy` を公開するまでの e2e の入口で、公開時に消す。利用者向けの
//! コマンドではない (順序の保証・確認・後始末をしない)。

use anyhow::{Context as _, Result};
use aws_config::SdkConfig;
use clap::Subcommand;
use std::collections::BTreeMap;

use magic_pocket_settings::context::{
    BackupContext, ContainerContext, Context, DsqlContext, RdsContext, VpcContext,
};

use super::backup::{AwsBackup, BackupDeploy};
use super::cloudfront::{AwsCloudFront, CloudFrontDeploy, CloudFrontWaits};
use super::container::{AwsContainer, ContainerAws, ContainerDeploy, Waits};
use super::dsql::{AwsDsql, DsqlDeploy};
use super::ecr::BuildEnv;
use super::edge::find_cloudfront;
use super::efs::AwsEfs;
use super::inbound::{AwsInbound, InboundDeploy, InboundWaits};
use super::inbound_domain::{
    cleanup_unused_inbound_domains, AwsInboundDomain, InboundDomainDeploy, InboundDomainWaits,
};
use super::lambda::AwsLambda;
use super::mediator::Mediator;
use super::neon::Neon;
use super::provider_urls::{deploy_managed, Provider, ProviderUrls};
use super::rds::{AwsRds, RdsAws, RdsDeploy, RdsWaits};
use super::s3::{BucketWriter, CreateRetry};
use super::secret_store::AwsSecretStore;
use super::stacks::AwsStacks;
use super::stored_value::AwsStoredValues;
use super::tidb::{MysqlSql, TiDb};
use super::upload::Uploader;
use super::upstash::Upstash;
use super::vpc::{AwsVpcNetwork, VpcDeploy, VpcWaits};
use crate::aws_util::regional_config;
use crate::context::load_context;
use crate::image_build::runner::SystemRunner;
use crate::stack::ops::{wait_status, CfnClient, ResourceStatus, StackApi, WaitOptions};
use crate::stack::roles;
use crate::status::providers::Http;
use crate::status::sink::Terminal;

#[derive(Subcommand)]
pub enum DeployPart {
    /// managed secret を用意する (引き継ぎ・生成・不要な key の削除・user secret の確認)
    Secrets {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// API で作る role (CodeBuild / AWS Backup) を作る・消す
    Roles {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        #[arg(long)]
        delete: bool,
    },
    /// container の stack を作る前の用意 (image と外部 VPC の検査)
    ContainerInit {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        #[arg(long)]
        container: String,
        /// build せず、この tag の image に stage の tag を付け替える
        #[arg(long)]
        promote: Option<String>,
    },
    /// container の stack を作る
    ContainerCreate {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        #[arg(long)]
        container: String,
    },
    /// container を更新する (image の差し替え・stack の更新・DEPLOY_HASH の同期)
    ContainerUpdate {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        #[arg(long)]
        container: String,
    },
    /// container の stack・ECR repository・container store の managed secret を消す
    ContainerDelete {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        #[arg(long)]
        container: String,
    },
    /// container の stack の template を stdout に出す
    ContainerTemplate {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        #[arg(long)]
        container: String,
    },
    /// S3 bucket を作る (在れば設定を合わせる)
    S3 {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// S3 bucket を中身ごと消す
    S3Delete {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// cloudfront の stack の template を stdout に出す
    CloudfrontTemplate {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        #[arg(long)]
        cloudfront: String,
    },
    /// cloudfront の stack の外の状態 (bucket policy / KVS) を合わせる
    CloudfrontPost {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        #[arg(long)]
        cloudfront: String,
    },
    /// bucket policy から statement を外し、cloudfront の stack を消す
    CloudfrontDelete {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        #[arg(long)]
        cloudfront: String,
    },
    /// upload_dir のある route を build して upload する
    Upload {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        #[arg(long)]
        cloudfront: String,
        #[arg(long)]
        skip_build: bool,
    },
    /// managed_assets を upload する
    ManagedAssets {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        #[arg(long)]
        cloudfront: String,
    },
    /// managed VPC の stack の前の用意 (EFS の確保)
    VpcInit {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// managed VPC の stack を作る (完了は待たない)
    VpcCreate {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// managed VPC の stack を更新する (template が変わっていれば)
    VpcUpdate {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// managed VPC の stack と EFS を消す
    VpcDelete {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// RDS の作成前の検査 (外部 VPC の stack)
    RdsInit {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// RDS の cluster を作る
    RdsCreate {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// RDS の cluster を望む設定に合わせる
    RdsUpdate {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// RDS の cluster を消す (最終 snapshot を取る)
    RdsDelete {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// DSQL の backup 未宣言の警告
    DsqlInit {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// DSQL の cluster を作る
    DsqlCreate {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// DSQL の endpoint の公開を合わせる
    DsqlPost {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// DSQL の cluster を消す
    DsqlDelete {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// backup の plan / selection を確保する
    BackupPost {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// backup の plan / selection / role を消す
    BackupDelete {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// Neon の branch / role / database を確保する (provisioning = "command" なら何もしない)
    NeonCreate {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// TiDB の cluster / database を確保する (provisioning = "command" なら何もしない)
    TidbCreate {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// Upstash の database を確保する (provisioning = "command" なら何もしない)
    UpstashCreate {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// inbound の deploy 前の検査 (rule set の差し込み先・変えられない設定)
    InboundPrepare {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// inbound の stack を作る
    InboundCreate {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// inbound の stack を更新する (template が変わっていれば)
    InboundUpdate {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// inbound の deploy 後の確認 (SES の rule が宣言と一致するか)
    InboundPost {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// inbound を消す (受信停止・配送猶予・滞留なしを確かめてから)
    InboundDelete {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        #[arg(long)]
        name: String,
    },
    /// 宣言から消えた inbound の stack が残っていれば止める
    InboundCheckRemoved {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        /// state の JSON (`resources.inbound` を読む)
        #[arg(long)]
        state: std::path::PathBuf,
    },
    /// 受信ドメインの deploy 前の検査 (pocket が作っていない identity を拒否する)
    InboundDomainPrepare {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// 受信ドメインの stack を作る
    InboundDomainCreate {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// 受信ドメインの stack を更新する (template が変わっていれば)
    InboundDomainUpdate {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// 受信ドメインの検証を確定する (manage_dns は待つ。外部 DNS は record を示す)
    InboundDomainConfirm {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// 受信ドメインの stack を消す
    InboundDomainDelete {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// どの inbound も使わなくなった受信ドメインの stack を消す (完了は待たない)
    InboundDomainCleanup {
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        /// state の JSON (`resources.inbound_domain` を読む)
        #[arg(long)]
        state: std::path::PathBuf,
    },
}

pub async fn run(part: DeployPart) -> Result<()> {
    let sdk = aws_config::defaults(aws_config::BehaviorVersion::latest())
        .load()
        .await;
    let mut sink = Terminal::new();
    match part {
        DeployPart::Secrets { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let store = AwsSecretStore::new(sdk.clone());
            let urls = ProviderUrls::new()?;
            let mediator = Mediator {
                ctx: &ctx,
                store: &store,
                urls: &urls,
            };
            mediator
                .ensure_pocket_managed_secrets(&mut sink, || Ok(load_context(&stage)?.0))
                .await?;
            Ok(())
        }
        DeployPart::Roles { stage, delete } => roles_part(&sdk, &stage, delete).await,
        DeployPart::ContainerInit {
            stage,
            container,
            promote,
        } => {
            let (ctx, toml_dir) = load_context(&stage)?;
            let mut c = find(&ctx, &container)?.clone();
            c.promote_commit_hash = promote;
            let env = Env::new(&sdk, &ctx, &toml_dir).await?;
            let aws = env.aws();
            env.deploy(&aws).deploy_init(&c).await
        }
        DeployPart::ContainerCreate { stage, container } => {
            let (ctx, toml_dir) = load_context(&stage)?;
            let env = Env::new(&sdk, &ctx, &toml_dir).await?;
            let aws = env.aws();
            env.deploy(&aws)
                .create(&ctx, &container, &mut sink, || Ok(load_context(&stage)?.0))
                .await
        }
        DeployPart::ContainerUpdate { stage, container } => {
            let (ctx, toml_dir) = load_context(&stage)?;
            let env = Env::new(&sdk, &ctx, &toml_dir).await?;
            let aws = env.aws();
            env.deploy(&aws)
                .update(&ctx, &container, &mut sink, || Ok(load_context(&stage)?.0))
                .await
        }
        DeployPart::ContainerDelete { stage, container } => {
            let (ctx, _) = load_context(&stage)?;
            let c = find(&ctx, &container)?;
            let name = crate::stack::container::stack_name(c);
            let cfn = CfnClient::new(&sdk, &c.region);
            if cfn.describe(&name).await?.is_some() {
                cfn.delete(&name).await?;
                wait_status(
                    &cfn,
                    &name,
                    "container",
                    ResourceStatus::NoExist,
                    WaitOptions::default(),
                )
                .await?;
            }
            let ecr = aws_sdk_ecr::Client::new(&crate::aws_util::regional_config(&sdk, &c.region));
            if super::ecr::delete_repository(&ecr, &c.ecr_name).await? {
                println!("{}: deleted", c.ecr_name);
            }
            if let Some(view) = &c.secrets {
                AwsSecretStore::new(sdk.clone()).delete_all(view).await?;
            }
            Ok(())
        }
        DeployPart::ContainerTemplate { stage, container } => {
            let (ctx, toml_dir) = load_context(&stage)?;
            let env = Env::new(&sdk, &ctx, &toml_dir).await?;
            let stack = env
                .aws()
                .container_template(&ctx, find(&ctx, &container)?)
                .await?;
            print!("{}", stack.body);
            Ok(())
        }
        DeployPart::S3 { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let s3 = ctx
                .s3
                .as_ref()
                .context("s3 がこの stage に設定されていません。")?;
            let client = aws_sdk_s3::Client::new(&regional_config(&sdk, &s3.region));
            let writer = BucketWriter {
                client: &client,
                s3,
                cloudfront: &ctx.cloudfront,
                retry: CreateRetry::default(),
            };
            if writer.exists().await? {
                writer.update(&mut sink).await
            } else {
                writer.create(&mut sink).await
            }
        }
        DeployPart::S3Delete { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let s3 = ctx
                .s3
                .as_ref()
                .context("s3 がこの stage に設定されていません。")?;
            let client = aws_sdk_s3::Client::new(&regional_config(&sdk, &s3.region));
            let deleted = super::s3::delete_bucket_with_contents(&client, &s3.bucket_name).await?;
            println!(
                "{}: {}",
                s3.bucket_name,
                if deleted { "deleted" } else { "not found" }
            );
            Ok(())
        }
        DeployPart::CloudfrontTemplate { stage, cloudfront } => {
            let (ctx, _) = load_context(&stage)?;
            let (stacks, aws, store) = cloudfront_env(&sdk);
            let stack = cloudfront_deploy(&stacks, &aws, &store)
                .template(&ctx, &cloudfront, &mut sink)
                .await?;
            print!("{}", stack.body);
            Ok(())
        }
        DeployPart::CloudfrontPost { stage, cloudfront } => {
            let (ctx, _) = load_context(&stage)?;
            let (stacks, aws, store) = cloudfront_env(&sdk);
            cloudfront_deploy(&stacks, &aws, &store)
                .ensure_post_deploy_state(&ctx, &cloudfront, &mut sink)
                .await
        }
        DeployPart::CloudfrontDelete { stage, cloudfront } => {
            let (ctx, _) = load_context(&stage)?;
            let (stacks, aws, store) = cloudfront_env(&sdk);
            let cf = find_cloudfront(&ctx, &cloudfront)?;
            cloudfront_deploy(&stacks, &aws, &store)
                .delete(cf, &mut sink)
                .await
        }
        DeployPart::Upload {
            stage,
            cloudfront,
            skip_build,
        } => {
            let (ctx, toml_dir) = load_context(&stage)?;
            let cf = find_cloudfront(&ctx, &cloudfront)?;
            let (stacks, aws, store) = cloudfront_env(&sdk);
            let distribution_id = cloudfront_deploy(&stacks, &aws, &store)
                .distribution_id(cf)
                .await?;
            let client = aws_sdk_s3::Client::new(&regional_config(&sdk, &cf.s3_region));
            let uploader = Uploader {
                sdk: &sdk,
                client: &client,
                cf,
                project_dir: &toml_dir,
            };
            let routes: Vec<_> = cf.uploadable_routes.iter().collect();
            uploader
                .upload(&routes, skip_build, &distribution_id, &mut sink)
                .await
        }
        DeployPart::ManagedAssets { stage, cloudfront } => {
            let (ctx, toml_dir) = load_context(&stage)?;
            let cf = find_cloudfront(&ctx, &cloudfront)?;
            let client = aws_sdk_s3::Client::new(&regional_config(&sdk, &cf.s3_region));
            let uploader = Uploader {
                sdk: &sdk,
                client: &client,
                cf,
                project_dir: &toml_dir,
            };
            uploader.upload_managed_assets(&mut sink).await
        }
        DeployPart::VpcInit { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let (stacks, efs, network) = vpc_env(&sdk);
            let deploy = vpc_deploy(&stacks, &efs, &network);
            for vpc in managed_vpcs(&ctx) {
                deploy.deploy_init(vpc).await?;
            }
            Ok(())
        }
        DeployPart::VpcCreate { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let (stacks, efs, network) = vpc_env(&sdk);
            let deploy = vpc_deploy(&stacks, &efs, &network);
            for vpc in managed_vpcs(&ctx) {
                deploy.create(vpc).await?;
            }
            Ok(())
        }
        DeployPart::VpcUpdate { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let (stacks, efs, network) = vpc_env(&sdk);
            let deploy = vpc_deploy(&stacks, &efs, &network);
            for vpc in managed_vpcs(&ctx) {
                deploy.update(vpc).await?;
            }
            Ok(())
        }
        DeployPart::VpcDelete { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let (stacks, efs, network) = vpc_env(&sdk);
            let deploy = vpc_deploy(&stacks, &efs, &network);
            for vpc in managed_vpcs(&ctx) {
                deploy.delete(vpc).await?;
            }
            Ok(())
        }
        DeployPart::RdsInit { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let rds = rds_context(&ctx)?;
            let (aws, stacks, values) = rds_env(&sdk, &rds.region);
            rds_deploy(&aws, &stacks, &values).deploy_init(rds).await
        }
        DeployPart::RdsCreate { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let rds = rds_context(&ctx)?;
            let (aws, stacks, values) = rds_env(&sdk, &rds.region);
            rds_deploy(&aws, &stacks, &values)
                .create(rds, &mut sink)
                .await
        }
        DeployPart::RdsUpdate { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let rds = rds_context(&ctx)?;
            let (aws, stacks, values) = rds_env(&sdk, &rds.region);
            rds_deploy(&aws, &stacks, &values)
                .update(rds, &mut sink)
                .await
        }
        DeployPart::RdsDelete { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let rds = rds_context(&ctx)?;
            let (aws, stacks, values) = rds_env(&sdk, &rds.region);
            rds_deploy(&aws, &stacks, &values)
                .delete(rds, &mut sink)
                .await
        }
        DeployPart::DsqlInit { stage } => {
            let (ctx, _) = load_context(&stage)?;
            super::dsql::deploy_init(dsql_context(&ctx)?, &mut sink);
            Ok(())
        }
        DeployPart::DsqlCreate { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let (api, values) = (AwsDsql { sdk: &sdk }, AwsStoredValues { sdk: &sdk });
            dsql_deploy(&api, &values)
                .create(dsql_context(&ctx)?, &mut sink)
                .await
        }
        DeployPart::DsqlPost { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let (api, values) = (AwsDsql { sdk: &sdk }, AwsStoredValues { sdk: &sdk });
            dsql_deploy(&api, &values)
                .ensure_post_deploy_state(dsql_context(&ctx)?, &mut sink)
                .await
        }
        DeployPart::DsqlDelete { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let (api, values) = (AwsDsql { sdk: &sdk }, AwsStoredValues { sdk: &sdk });
            dsql_deploy(&api, &values)
                .delete(dsql_context(&ctx)?, &mut sink)
                .await
        }
        DeployPart::BackupPost { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let backup = backup_context(&ctx)?;
            let targets = backup_targets(&sdk, &ctx).await?;
            let api = AwsBackup {
                sdk: &sdk,
                region: &backup.region,
            };
            BackupDeploy { api: &api }
                .ensure_post_deploy_state(backup, &targets, &mut sink)
                .await
        }
        DeployPart::BackupDelete { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let backup = backup_context(&ctx)?;
            let api = AwsBackup {
                sdk: &sdk,
                region: &backup.region,
            };
            BackupDeploy { api: &api }.delete(backup, &mut sink).await
        }
        DeployPart::NeonCreate { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let Some(neon) = provider(&ctx, Provider::Neon, ctx.neon.as_ref()) else {
                return Ok(());
            };
            let http = Http::new()?;
            let neon = Neon {
                http: &http,
                ctx: neon,
            };
            neon.create(&mut sink).await
        }
        DeployPart::TidbCreate { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let Some(tidb) = provider(&ctx, Provider::Tidb, ctx.tidb.as_ref()) else {
                return Ok(());
            };
            let http = Http::new()?;
            TiDb::new(&http, tidb, &MysqlSql).create().await
        }
        DeployPart::UpstashCreate { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let Some(upstash) = provider(&ctx, Provider::Upstash, ctx.upstash.as_ref()) else {
                return Ok(());
            };
            let http = Http::new()?;
            let upstash = Upstash {
                http: &http,
                ctx: upstash,
            };
            upstash.create(&mut sink).await
        }
        DeployPart::InboundPrepare { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let (stacks, aws) = (AwsStacks { sdk: &sdk }, AwsInbound { sdk: &sdk });
            let deploy = inbound_deploy(&stacks, &aws);
            for inlet in ctx.inbound.values() {
                deploy.prepare_deploy(inlet).await?;
            }
            Ok(())
        }
        DeployPart::InboundCreate { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let (stacks, aws) = (AwsStacks { sdk: &sdk }, AwsInbound { sdk: &sdk });
            let deploy = inbound_deploy(&stacks, &aws);
            for inlet in ctx.inbound.values() {
                deploy.create(inlet).await?;
            }
            Ok(())
        }
        DeployPart::InboundUpdate { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let (stacks, aws) = (AwsStacks { sdk: &sdk }, AwsInbound { sdk: &sdk });
            let deploy = inbound_deploy(&stacks, &aws);
            for inlet in ctx.inbound.values() {
                deploy.update(inlet).await?;
            }
            Ok(())
        }
        DeployPart::InboundPost { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let (stacks, aws) = (AwsStacks { sdk: &sdk }, AwsInbound { sdk: &sdk });
            let deploy = inbound_deploy(&stacks, &aws);
            for inlet in ctx.inbound.values() {
                deploy.ensure_post_deploy_state(inlet).await?;
            }
            Ok(())
        }
        DeployPart::InboundDelete { stage, name } => {
            let (ctx, _) = load_context(&stage)?;
            let inlet = ctx
                .inbound
                .get(&name)
                .with_context(|| format!("inbound '{name}' がありません"))?;
            let (stacks, aws) = (AwsStacks { sdk: &sdk }, AwsInbound { sdk: &sdk });
            inbound_deploy(&stacks, &aws).delete(inlet).await
        }
        DeployPart::InboundCheckRemoved { stage, state } => {
            let (ctx, _) = load_context(&stage)?;
            let saved = saved_resources(&state, "inbound")?;
            super::inbound::check_removed_inbound(&ctx, &saved, &AwsStacks { sdk: &sdk }).await
        }
        DeployPart::InboundDomainPrepare { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let (stacks, aws) = (AwsStacks { sdk: &sdk }, AwsInboundDomain { sdk: &sdk });
            let deploy = inbound_domain_deploy(&stacks, &aws);
            for domain in ctx.inbound_domain.values() {
                deploy.prepare_deploy(domain).await?;
            }
            Ok(())
        }
        DeployPart::InboundDomainCreate { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let (stacks, aws) = (AwsStacks { sdk: &sdk }, AwsInboundDomain { sdk: &sdk });
            let deploy = inbound_domain_deploy(&stacks, &aws);
            for domain in ctx.inbound_domain.values() {
                deploy.create(domain).await?;
            }
            Ok(())
        }
        DeployPart::InboundDomainUpdate { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let (stacks, aws) = (AwsStacks { sdk: &sdk }, AwsInboundDomain { sdk: &sdk });
            let deploy = inbound_domain_deploy(&stacks, &aws);
            for domain in ctx.inbound_domain.values() {
                deploy.update(domain).await?;
            }
            Ok(())
        }
        DeployPart::InboundDomainConfirm { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let (stacks, aws) = (AwsStacks { sdk: &sdk }, AwsInboundDomain { sdk: &sdk });
            inbound_domain_deploy(&stacks, &aws)
                .confirm(&ctx, &mut sink)
                .await
        }
        DeployPart::InboundDomainDelete { stage } => {
            let (ctx, _) = load_context(&stage)?;
            let (stacks, aws) = (AwsStacks { sdk: &sdk }, AwsInboundDomain { sdk: &sdk });
            let deploy = inbound_domain_deploy(&stacks, &aws);
            for domain in ctx.inbound_domain.values() {
                deploy.delete(domain).await?;
            }
            Ok(())
        }
        DeployPart::InboundDomainCleanup { stage, state } => {
            let (ctx, _) = load_context(&stage)?;
            let saved = saved_resources(&state, "inbound_domain")?;
            let stacks = AwsStacks { sdk: &sdk };
            cleanup_unused_inbound_domains(&ctx, &saved, &stacks, &mut sink).await
        }
    }
}

/// state の JSON の `resources.<kind>` (無ければ null)
fn saved_resources(path: &std::path::Path, kind: &str) -> Result<serde_json::Value> {
    let text = std::fs::read_to_string(path)
        .with_context(|| format!("{} を読めません", path.display()))?;
    let mut state: serde_json::Value = serde_json::from_str(&text)
        .with_context(|| format!("{} が JSON ではありません", path.display()))?;
    Ok(state["resources"][kind].take())
}

fn inbound_deploy<'a>(
    stacks: &'a AwsStacks<'a>,
    aws: &'a AwsInbound<'a>,
) -> InboundDeploy<'a, AwsStacks<'a>, AwsInbound<'a>> {
    InboundDeploy {
        stacks,
        aws,
        waits: InboundWaits::default(),
        now: std::time::SystemTime::now,
    }
}

fn inbound_domain_deploy<'a>(
    stacks: &'a AwsStacks<'a>,
    aws: &'a AwsInboundDomain<'a>,
) -> InboundDomainDeploy<'a, AwsStacks<'a>, AwsInboundDomain<'a>> {
    InboundDomainDeploy {
        stacks,
        aws,
        waits: InboundDomainWaits::default(),
    }
}

/// deploy が作る provider の設定。deploy の対象でなければ、その旨を出して `None`
fn provider<'c, T>(ctx: &Context, kind: Provider, settings: Option<&'c T>) -> Option<&'c T> {
    let settings = settings.filter(|_| deploy_managed(ctx).contains(&kind));
    if settings.is_none() {
        println!("{kind:?}: skipped (宣言が無いか、provisioning = \"command\")");
    }
    settings
}

/// stage が管理する VPC (container と RDS が使うもの。名前の重複を除く)
fn managed_vpcs(ctx: &Context) -> Vec<&VpcContext> {
    let container_vpcs = ctx.container.values().filter_map(|c| c.vpc.as_ref());
    let rds_vpc = ctx.rds.as_ref().and_then(|rds| rds.vpc.as_ref());
    let mut vpcs: Vec<&VpcContext> = Vec::new();
    for vpc in container_vpcs.chain(rds_vpc) {
        if vpc.manage && !vpcs.iter().any(|known| known.name == vpc.name) {
            vpcs.push(vpc);
        }
    }
    vpcs
}

fn vpc_env(sdk: &SdkConfig) -> (AwsStacks<'_>, AwsEfs<'_>, AwsVpcNetwork<'_>) {
    (AwsStacks { sdk }, AwsEfs { sdk }, AwsVpcNetwork { sdk })
}

fn vpc_deploy<'a>(
    stacks: &'a AwsStacks<'a>,
    efs: &'a AwsEfs<'a>,
    network: &'a AwsVpcNetwork<'a>,
) -> VpcDeploy<'a, AwsStacks<'a>, AwsEfs<'a>, AwsVpcNetwork<'a>> {
    VpcDeploy {
        stacks,
        efs,
        network,
        waits: VpcWaits::default(),
    }
}

fn rds_context(ctx: &Context) -> Result<&RdsContext> {
    ctx.rds
        .as_ref()
        .context("rds がこの stage に設定されていません。")
}

fn rds_env<'a>(
    sdk: &'a SdkConfig,
    region: &'a str,
) -> (AwsRds<'a>, AwsStacks<'a>, AwsStoredValues<'a>) {
    (
        AwsRds { sdk, region },
        AwsStacks { sdk },
        AwsStoredValues { sdk },
    )
}

fn rds_deploy<'a>(
    aws: &'a AwsRds<'a>,
    stacks: &'a AwsStacks<'a>,
    values: &'a AwsStoredValues<'a>,
) -> RdsDeploy<'a, AwsRds<'a>, AwsStacks<'a>, AwsStoredValues<'a>> {
    RdsDeploy {
        aws,
        stacks,
        values,
        waits: RdsWaits::default(),
    }
}

fn dsql_context(ctx: &Context) -> Result<&DsqlContext> {
    ctx.dsql
        .as_ref()
        .context("dsql がこの stage に設定されていません。")
}

fn dsql_deploy<'a>(
    api: &'a AwsDsql<'a>,
    values: &'a AwsStoredValues<'a>,
) -> DsqlDeploy<'a, AwsDsql<'a>, AwsStoredValues<'a>> {
    DsqlDeploy {
        api,
        values,
        wait: super::dsql::default_wait(),
    }
}

fn backup_context(ctx: &Context) -> Result<&BackupContext> {
    ctx.backup
        .as_ref()
        .context("backup がこの stage に設定されていません。")
}

/// backup の対象 (service → cluster の ARN)。cluster が未作成の service と、
/// pocket が管理しない RDS (managed = false) は入れない
async fn backup_targets(sdk: &SdkConfig, ctx: &Context) -> Result<BTreeMap<String, String>> {
    let mut targets = BTreeMap::new();
    if let Some(dsql) = ctx.dsql.as_ref() {
        let found = super::dsql::find_cluster(sdk, &dsql.region, &dsql.tag_name).await?;
        if let Some(cluster) = found {
            targets.insert("dsql".to_string(), cluster.arn);
        }
    }
    if let Some(rds) = ctx.rds.as_ref().filter(|rds| rds.managed) {
        let aws = AwsRds {
            sdk,
            region: &rds.region,
        };
        let cluster = aws.cluster(&rds.cluster_identifier).await?;
        if let Some(arn) = cluster.and_then(|cluster| cluster.arn) {
            targets.insert("rds".to_string(), arn);
        }
    }
    Ok(targets)
}

fn cloudfront_env(sdk: &SdkConfig) -> (AwsStacks<'_>, AwsCloudFront<'_>, AwsSecretStore) {
    (
        AwsStacks { sdk },
        AwsCloudFront { sdk },
        AwsSecretStore::new(sdk.clone()),
    )
}

fn cloudfront_deploy<'a>(
    stacks: &'a AwsStacks<'a>,
    aws: &'a AwsCloudFront<'a>,
    store: &'a AwsSecretStore,
) -> CloudFrontDeploy<'a, AwsStacks<'a>, AwsCloudFront<'a>, AwsSecretStore> {
    CloudFrontDeploy {
        stacks,
        aws,
        store,
        waits: CloudFrontWaits::default(),
    }
}

fn find<'c>(ctx: &'c Context, container: &str) -> Result<&'c ContainerContext> {
    ctx.container
        .get(container)
        .with_context(|| format!("container '{container}' がありません"))
}

/// AWS に繋いだ [`ContainerDeploy`] の材料
struct Env<'a> {
    sdk: &'a SdkConfig,
    context_dir: std::path::PathBuf,
    account_id: String,
    state_bucket: String,
    region: String,
    lambda: AwsLambda,
    store: AwsSecretStore,
    urls: ProviderUrls,
}

impl<'a> Env<'a> {
    async fn new(sdk: &'a SdkConfig, ctx: &Context, toml_dir: &std::path::Path) -> Result<Self> {
        let general = ctx
            .general
            .as_ref()
            .context("general context is not configured")?;
        Ok(Self {
            sdk,
            context_dir: std::fs::canonicalize(toml_dir)
                .with_context(|| format!("{} を解決できません", toml_dir.display()))?,
            account_id: crate::image_build::check_aws_credentials(sdk).await?,
            state_bucket: roles::state_bucket_name(ctx).unwrap_or_default(),
            region: general.region.clone(),
            lambda: AwsLambda::new(sdk, &general.region),
            store: AwsSecretStore::new(sdk.clone()),
            urls: ProviderUrls::new()?,
        })
    }

    fn aws(&self) -> AwsContainer<'_> {
        AwsContainer {
            build: BuildEnv {
                sdk: self.sdk,
                context_dir: &self.context_dir,
                account_id: &self.account_id,
                state_bucket: &self.state_bucket,
                state_bucket_region: &self.region,
                runner: &SystemRunner,
            },
        }
    }

    fn deploy<'d>(
        &'d self,
        aws: &'d AwsContainer<'d>,
    ) -> ContainerDeploy<'d, AwsContainer<'d>, AwsLambda, AwsSecretStore, ProviderUrls> {
        ContainerDeploy {
            aws,
            lambda: &self.lambda,
            store: &self.store,
            urls: &self.urls,
            waits: Waits::default(),
        }
    }
}

/// API で作る role の service
const API_ROLE_SERVICES: &[&str] = &["codebuild.amazonaws.com", "backup.amazonaws.com"];

async fn roles_part(sdk: &SdkConfig, stage: &str, delete: bool) -> Result<()> {
    let (ctx, _) = load_context(stage)?;
    let general = ctx
        .general
        .as_ref()
        .context("general context is not configured")?;
    let account_id = crate::image_build::check_aws_credentials(sdk).await?;
    let boundary = std::env::var("CODEBUILD_PERMISSIONS_BOUNDARY").ok();
    let iam = aws_sdk_iam::Client::new(&crate::aws_util::regional_config(sdk, &general.region));
    for spec in roles::stage_role_specs(&ctx, &account_id, boundary.as_deref())? {
        if !API_ROLE_SERVICES.contains(&spec.service.as_str()) {
            continue;
        }
        if delete {
            let deleted =
                super::iam::delete_role(&iam, &spec.name, &spec.managed_policy_arns).await?;
            println!(
                "{}: {}",
                spec.name,
                if deleted { "deleted" } else { "not found" }
            );
        } else {
            let arn = super::iam::ensure_role(&iam, &spec).await?;
            println!("{}: {arn}", spec.name);
        }
    }
    Ok(())
}
