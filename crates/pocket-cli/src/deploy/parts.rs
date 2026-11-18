//! deploy の部品を 1 つずつ実行する非表示サブコマンド (`pocket-rs deploy-parts`)。
//!
//! `pocket-rs deploy` を公開するまでの e2e の入口で、公開時に消す。利用者向けの
//! コマンドではない (順序の保証・確認・後始末をしない)。

use anyhow::{bail, Context as _, Result};
use aws_config::SdkConfig;
use clap::Subcommand;
use magic_pocket_settings::context::{ContainerContext, Context};

use super::cloudfront::{AwsCloudFront, CloudFrontDeploy, CloudFrontWaits};
use super::container::{AwsContainer, ContainerAws, ContainerDeploy, Waits};
use super::ecr::BuildEnv;
use super::edge::find_cloudfront;
use super::lambda::AwsLambda;
use super::mediator::{DatabaseUrls, Mediator};
use super::s3::{BucketWriter, CreateRetry};
use super::secret_store::AwsSecretStore;
use super::stacks::AwsStacks;
use super::upload::Uploader;
use crate::aws_util::regional_config;
use crate::context::load_context;
use crate::image_build::runner::SystemRunner;
use crate::stack::ops::{wait_status, CfnClient, ResourceStatus, StackApi, WaitOptions};
use crate::stack::roles;
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
}

/// 管理 API から算出する URL (computed mode) は、provider の移植と一緒に結合する
struct UrlsNotPorted;

impl DatabaseUrls for UrlsNotPorted {
    async fn neon_database_url(&self, _ctx: &Context) -> Result<Option<String>> {
        bail!("neon_database_url (computed) は pocket-rs では未対応です。")
    }
    async fn tidb_database_url(&self, _ctx: &Context) -> Result<Option<String>> {
        bail!("tidb_database_url (computed) は pocket-rs では未対応です。")
    }
    async fn upstash_redis_url(&self, _ctx: &Context) -> Result<Option<String>> {
        bail!("upstash_redis_url (computed) は pocket-rs では未対応です。")
    }
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
            let mediator = Mediator {
                ctx: &ctx,
                store: &store,
                urls: &UrlsNotPorted,
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
    }
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
    ) -> ContainerDeploy<'d, AwsContainer<'d>, AwsLambda, AwsSecretStore, UrlsNotPorted> {
        ContainerDeploy {
            aws,
            lambda: &self.lambda,
            store: &self.store,
            urls: &UrlsNotPorted,
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
