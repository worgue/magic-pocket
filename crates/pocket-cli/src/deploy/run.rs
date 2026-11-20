//! `pocket-rs deploy` / `promote` の本体 (Python 版 `cli/deploy_cli.py` の `run_deploy` /
//! `run_promote`)。手順の順は [`super::pipeline`]、ここは各手順を AWS の部品につなぐ

use std::collections::BTreeSet;
use std::path::PathBuf;

use anyhow::{bail, Context as _, Result};
use aws_config::SdkConfig;
use magic_pocket_settings::context::{CloudFrontContext, Context};
use magic_pocket_settings::types::OrderedMap;
use serde_json::Value as Json;

use super::backup::{AwsBackup, BackupDeploy};
use super::edge::{EdgeDeploy, EdgeWaits};
use super::inbound::{check_removed_inbound, AwsInbound};
use super::inbound_domain::{cleanup_unused_inbound_domains, AwsInboundDomain};
use super::interaction::Interaction;
use super::migrations;
use super::parts;
use super::pipeline::{self, DeployOptions, Ops, Phase};
use super::s3::{BucketWriter, CreateRetry};
use super::secret_store::AwsSecretStore;
use super::stacks::{AwsStacks, Stacks};
use super::state::StateStore;
use super::stored_value::AwsStoredValues;
use super::upload::Uploader;
use super::vpc::AwsVpcNetwork;
use super::{dsql, efs::AwsEfs, lambda};
use crate::aws_util::regional_config;
use crate::context::load_context;
use crate::image_build::context_files::world_read_warning_summary;
use crate::stack::cloudfront as cf_stack;
use crate::stack::ops::{cfn_status, stack_outputs, ResourceStatus};
use crate::status::aws::AwsEvaluator;
use crate::status::report::Evaluator as _;
use crate::status::resource::{resources, Resource};
use crate::status::sink::{Sink, Terminal};

/// `deploy` / `promote` の引数
pub struct DeployArgs {
    pub stage: String,
    /// 昇格する image の commit hash (`promote`)。`None` なら build する
    pub promote: Option<String>,
    pub openpath: Option<String>,
    pub skip_frontend: bool,
    pub yes: bool,
}

/// Context を読む。昇格のときは全 container に commit hash を入れる
fn load(stage: &str, promote: Option<&str>) -> Result<(Context, PathBuf)> {
    let (mut ctx, toml_dir) = load_context(stage)?;
    if let Some(hash) = promote {
        let mut containers = OrderedMap::default();
        for (name, container) in ctx.container.iter() {
            let mut container = container.clone();
            container.promote_commit_hash = Some(hash.to_string());
            containers.insert(name.clone(), container);
        }
        ctx.container = containers;
    }
    Ok((ctx, toml_dir))
}

pub async fn run(args: DeployArgs) -> Result<()> {
    let sdk = crate::aws_util::load_sdk().await;
    crate::image_build::check_aws_credentials(&sdk).await?;
    let (ctx, toml_dir) = load(&args.stage, args.promote.as_deref())?;
    if args.promote.is_some() && ctx.container.is_empty() {
        bail!("container がこの stage に設定されていません。");
    }
    let mut ops = AwsOps {
        state: StateStore::new(&sdk, &ctx)?,
        sdk,
        ctx,
        toml_dir,
        stage: args.stage,
        promote: args.promote,
        openpath: args.openpath,
        prompt: Interaction {
            assume_yes: args.yes,
        },
        uploaded: BTreeSet::new(),
    };
    let options = DeployOptions {
        skip_frontend: args.skip_frontend,
    };
    pipeline::run(&mut ops, options, &mut Terminal::new()).await
}

/// AWS に繋いだ [`Ops`]
struct AwsOps {
    sdk: SdkConfig,
    ctx: Context,
    toml_dir: PathBuf,
    stage: String,
    promote: Option<String>,
    openpath: Option<String>,
    state: StateStore,
    prompt: Interaction,
    /// Lambda の更新より先に upload した (cloudfront, route)
    uploaded: BTreeSet<(String, String)>,
}

impl AwsOps {
    fn reload(&self) -> impl FnOnce() -> Result<Context> + '_ {
        || Ok(load(&self.stage, self.promote.as_deref())?.0)
    }

    fn edge<'a>(
        &self,
        stacks: &'a AwsStacks<'a>,
        store: &'a AwsSecretStore,
    ) -> EdgeDeploy<'a, AwsStacks<'a>, AwsSecretStore> {
        EdgeDeploy {
            stacks,
            store,
            waits: EdgeWaits::default(),
        }
    }

    /// cloudfront の stack が在るか (状態は問わない)
    async fn cloudfront_exists(&self, cf: &CloudFrontContext) -> Result<bool> {
        let stacks = AwsStacks { sdk: &self.sdk };
        let name = cf_stack::cloudfront_stack_name(cf);
        let description = stacks.describe(&cf.region, &name).await?;
        Ok(cfn_status(description.as_ref())? != ResourceStatus::NoExist)
    }

    /// route を upload する。`versioned_only`: versioning のある route だけを、Lambda の
    /// 更新より先に上げる (stack が未作成なら何もせず、後の upload に任せる)
    async fn upload_routes(&mut self, versioned_only: bool, sink: &mut dyn Sink) -> Result<()> {
        let mut uploaded = Vec::new();
        for cf in self.ctx.cloudfront.values() {
            let routes: Vec<_> = cf
                .uploadable_routes
                .iter()
                .filter(|route| match versioned_only {
                    true => route.versioning.is_some(),
                    false => !self
                        .uploaded
                        .contains(&(cf.name.clone(), route.name.clone())),
                })
                .collect();
            if routes.is_empty() {
                continue;
            }
            if !self.cloudfront_exists(cf).await? {
                if !versioned_only {
                    sink.warning(&format!(
                        "CloudFront '{}' が未作成です。スキップします。",
                        cf.name
                    ));
                }
                continue;
            }
            if versioned_only {
                sink.log("versioning route を Lambda 更新より先に upload します...");
            }
            let (stacks, aws, store) = parts::cloudfront_env(&self.sdk);
            let distribution_id = parts::cloudfront_deploy(&stacks, &aws, &store)
                .distribution_id(cf)
                .await?;
            let client = aws_sdk_s3::Client::new(&regional_config(&self.sdk, &cf.s3_region));
            let uploader = Uploader {
                sdk: &self.sdk,
                client: &client,
                cf,
                project_dir: &self.toml_dir,
            };
            uploader
                .upload(&routes, false, &distribution_id, sink)
                .await?;
            uploaded.extend(routes.iter().map(|r| (cf.name.clone(), r.name.clone())));
        }
        if versioned_only {
            self.uploaded.extend(uploaded);
        }
        Ok(())
    }

    async fn upload_managed_assets(&self, sink: &mut dyn Sink) -> Result<()> {
        for cf in self.ctx.cloudfront.values() {
            if cf.managed_assets.as_deref().is_none_or(str::is_empty) {
                continue;
            }
            let client = aws_sdk_s3::Client::new(&regional_config(&self.sdk, &cf.s3_region));
            let uploader = Uploader {
                sdk: &self.sdk,
                client: &client,
                cf,
                project_dir: &self.toml_dir,
            };
            uploader.upload_managed_assets(sink).await?;
        }
        Ok(())
    }

    /// deploy の後に示す URL。CloudFront (独自ドメイン → 配られたドメイン) を優先し、
    /// 無ければ API Gateway の endpoint (wsgi を優先)
    async fn deploy_url(&self) -> Result<Option<String>> {
        let stacks = AwsStacks { sdk: &self.sdk };
        for cf in self.ctx.cloudfront.values() {
            if let Some(domain) = cf.domain.as_deref().filter(|d| !d.is_empty()) {
                return Ok(Some(format!("https://{domain}")));
            }
            let name = cf_stack::cloudfront_stack_name(cf);
            let description = stacks.describe(&cf.region, &name).await?;
            let outputs = stack_outputs(description.as_ref()).unwrap_or_default();
            if let Some(domain) = outputs.get("DistributionDomainName") {
                return Ok(Some(format!("https://{domain}")));
            }
        }
        let mut names: Vec<&String> = self.ctx.container.keys().collect();
        names.sort();
        for name in names {
            let c = self.ctx.container.get(name).expect("container");
            let stack_name = crate::stack::container::stack_name(c);
            let description = stacks.describe(&c.region, &stack_name).await?;
            let outputs = stack_outputs(description.as_ref()).unwrap_or_default();
            let endpoints = container_endpoints(c, &outputs);
            if let Some((_, url)) = endpoints.iter().find(|(key, _)| key == "wsgi") {
                return Ok(Some(url.clone()));
            }
            if let Some((_, url)) = endpoints.first() {
                return Ok(Some(url.clone()));
            }
        }
        Ok(None)
    }

    /// state の `resources.<kind>` (記録が無ければ null)
    async fn saved(&self, kind: &str) -> Result<Json> {
        Ok(self.state.load().await?["resources"][kind].take())
    }
}

/// API Gateway を持つ handler の (handler 名, URL)。stack が未作成の handler は入らない
pub fn container_endpoints(
    c: &magic_pocket_settings::context::ContainerContext,
    outputs: &crate::stack::StackOutputs,
) -> Vec<(String, String)> {
    let mut endpoints = Vec::new();
    for (key, handler) in c.handlers.iter() {
        let Some(api) = handler.apigateway.as_ref() else {
            continue;
        };
        if let Some(domain) = api.domain.as_deref().filter(|d| !d.is_empty()) {
            endpoints.push((key.clone(), format!("https://{domain}")));
            continue;
        }
        // Python の `key.capitalize()`: 先頭だけ大文字、残りは小文字
        let mut chars = key.chars();
        let capitalized: String = match chars.next() {
            Some(first) => first
                .to_uppercase()
                .chain(chars.flat_map(char::to_lowercase))
                .collect(),
            None => String::new(),
        };
        if let Some(endpoint) = outputs.get(&format!("{capitalized}ApiEndpoint")) {
            let host = endpoint.strip_prefix("https://").unwrap_or(endpoint);
            endpoints.push((key.clone(), format!("https://{host}")));
        }
    }
    endpoints
}

/// URL をブラウザで開く (開けなくても deploy は成功のまま)
pub(crate) fn open_browser(url: &str) {
    let opener = if cfg!(target_os = "macos") {
        "open"
    } else {
        "xdg-open"
    };
    let _ = std::process::Command::new(opener)
        .arg(url)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn();
}

impl Ops for AwsOps {
    fn names(&self) -> Vec<&'static str> {
        resources(&self.ctx)
            .iter()
            .map(Resource::class_name)
            .collect()
    }

    async fn phase(&mut self, phase: Phase, sink: &mut dyn Sink) -> Result<()> {
        let sdk = &self.sdk;
        match phase {
            Phase::InboundPrepare => {
                let (stacks, aws) = (AwsStacks { sdk }, AwsInbound { sdk });
                let deploy = parts::inbound_deploy(&stacks, &aws);
                for inlet in self.ctx.inbound.values() {
                    deploy.prepare_deploy(inlet).await?;
                }
                let aws = AwsInboundDomain { sdk };
                let deploy = parts::inbound_domain_deploy(&stacks, &aws);
                for domain in self.ctx.inbound_domain.values() {
                    deploy.prepare_deploy(domain).await?;
                }
                Ok(())
            }
            Phase::DeployHash => {
                let env_set = std::env::var("DEPLOY_HASH").is_ok_and(|v| !v.is_empty());
                if let Some(message) = deploy_hash_report(&self.ctx, env_set) {
                    sink.info(&message);
                }
                Ok(())
            }
            Phase::VerifyRoles => super::iam::verify_stage_roles(sdk, &self.ctx).await,
            Phase::EnsureStateBucket => self.state.ensure_bucket().await,
            Phase::CheckRemovedInbound => {
                let saved = self.saved("inbound").await?;
                check_removed_inbound(&self.ctx, &saved, &AwsStacks { sdk }).await
            }
            Phase::VersionedFrontend => self.upload_routes(true, sink).await,
            Phase::CleanupInboundDomains => {
                let saved = self.saved("inbound_domain").await?;
                cleanup_unused_inbound_domains(&self.ctx, &saved, &AwsStacks { sdk }, sink).await
            }
            Phase::MigrationCleanup => {
                let store = AwsSecretStore::new(sdk.clone());
                migrations::cleanup_legacy_secret_residue(&self.ctx, &store, &self.prompt, sink)
                    .await?;
                migrations::cleanup_legacy_container_resources(
                    &self.ctx,
                    &AwsStacks { sdk },
                    &AwsRepositories { sdk },
                    &self.prompt,
                    migrations::legacy_stack_wait(),
                    sink,
                )
                .await
            }
            Phase::ManagedAssets => self.upload_managed_assets(sink).await,
            Phase::Frontend => self.upload_routes(false, sink).await,
            Phase::WorldReadWarnings => {
                if let Some(summary) = world_read_warning_summary() {
                    sink.warning(&summary);
                }
                Ok(())
            }
            Phase::AlertWarnings => {
                AwsEvaluator::new(&self.ctx, sdk.clone())
                    .alert_warnings(sink)
                    .await
            }
            Phase::InboundDetails => {
                AwsEvaluator::new(&self.ctx, sdk.clone())
                    .inbound_details(sink)
                    .await
            }
            Phase::Url => {
                if let Some(url) = self.deploy_url().await? {
                    sink.success(&format!("url: {url}"));
                    if let Some(path) = self.openpath.as_deref() {
                        open_browser(&format!("{url}/{path}"));
                    }
                }
                Ok(())
            }
            Phase::ConfirmInboundDomains => {
                let (stacks, aws) = (AwsStacks { sdk }, AwsInboundDomain { sdk });
                parts::inbound_domain_deploy(&stacks, &aws)
                    .confirm(&self.ctx, sink)
                    .await
            }
        }
    }

    async fn deploy_init(&mut self, index: usize, sink: &mut dyn Sink) -> Result<()> {
        let sdk = &self.sdk;
        match &resources(&self.ctx)[index] {
            Resource::Vpc(vpc) => {
                let (stacks, efs, network) = parts::vpc_env(sdk);
                parts::vpc_deploy(&stacks, &efs, &network)
                    .deploy_init(vpc)
                    .await
            }
            Resource::Dsql(context) => {
                dsql::deploy_init(context, sink);
                Ok(())
            }
            Resource::Rds(rds) => {
                let (aws, stacks, values) = parts::rds_env(sdk, &rds.region);
                parts::rds_deploy(&aws, &stacks, &values)
                    .deploy_init(rds)
                    .await
            }
            Resource::Container(c) => {
                let env = parts::Env::new(sdk, &self.ctx, &self.toml_dir).await?;
                let aws = env.aws();
                env.deploy(&aws).deploy_init(c).await
            }
            Resource::CloudFront(cf) => {
                super::cloudfront::warn_contents(cf, sink);
                Ok(())
            }
            _ => Ok(()),
        }
    }

    async fn status(&mut self, index: usize, sink: &mut dyn Sink) -> Result<ResourceStatus> {
        let list = resources(&self.ctx);
        // 直前の書き込みを反映した状態を見るため、毎回読み直す
        let mut evaluator = AwsEvaluator::new(&self.ctx, self.sdk.clone());
        evaluator.status(&list[index], sink).await
    }

    async fn create(&mut self, index: usize, sink: &mut dyn Sink) -> Result<()> {
        self.write(index, true, sink).await
    }

    async fn update(&mut self, index: usize, sink: &mut dyn Sink) -> Result<()> {
        self.write(index, false, sink).await
    }

    async fn record(&mut self, index: usize) -> Result<()> {
        let list = resources(&self.ctx);
        let resource = &list[index];
        let extra = match resource {
            Resource::Dsql(context) => {
                dsql::find_cluster(&self.sdk, &context.region, &context.tag_name)
                    .await?
                    .map(|cluster| cluster.identifier)
            }
            Resource::Rds(rds) if rds.managed => {
                let config = regional_config(&self.sdk, &rds.region);
                crate::ec2::security_group_id(&config, &rds.security_group_name).await?
            }
            _ => None,
        };
        let info = pipeline::state_info(resource, extra.as_deref());
        self.state.record(&info).await
    }

    async fn ensure_post_deploy_state(&mut self, index: usize, sink: &mut dyn Sink) -> Result<()> {
        let sdk = &self.sdk;
        match &resources(&self.ctx)[index] {
            Resource::Dsql(context) => {
                let (api, values) = (dsql::AwsDsql { sdk }, AwsStoredValues { sdk });
                parts::dsql_deploy(&api, &values)
                    .ensure_post_deploy_state(context, sink)
                    .await
            }
            Resource::Backup(backup) => {
                let targets = parts::backup_targets(sdk, &self.ctx).await?;
                let api = AwsBackup {
                    sdk,
                    region: &backup.region,
                };
                BackupDeploy { api: &api }
                    .ensure_post_deploy_state(backup, &targets, sink)
                    .await
            }
            Resource::Container(c) => {
                let api = lambda::AwsLambda::new(sdk, &c.region);
                lambda::sync_deploy_hash(&api, sink, c, lambda::WaitUpdate::default()).await
            }
            Resource::Inbound(inlet) => {
                let (stacks, aws) = (AwsStacks { sdk }, AwsInbound { sdk });
                parts::inbound_deploy(&stacks, &aws)
                    .ensure_post_deploy_state(inlet)
                    .await
            }
            Resource::CloudFront(cf) => {
                let (stacks, aws, store) = parts::cloudfront_env(sdk);
                parts::cloudfront_deploy(&stacks, &aws, &store)
                    .ensure_post_deploy_state(&self.ctx, &cf.name, sink)
                    .await
            }
            _ => Ok(()),
        }
    }
}

impl AwsOps {
    /// リソースを作る (`create`) か更新する
    async fn write(&self, index: usize, create: bool, sink: &mut dyn Sink) -> Result<()> {
        let sdk = &self.sdk;
        let ctx = &self.ctx;
        let stacks = AwsStacks { sdk };
        let list = resources(ctx);
        let resource = &list[index];
        // 状態が REQUIRE_UPDATE にならないリソースの update
        let no_update = || -> Result<()> {
            bail!("{} は update に対応していません", resource.class_name())
        };
        match resource {
            Resource::CloudFrontAcm(cf) => {
                let store = AwsSecretStore::new(sdk.clone());
                let edge = self.edge(&stacks, &store);
                match create {
                    true => edge.acm_create(cf, sink).await,
                    false => edge.acm_update(cf).await,
                }
            }
            Resource::CloudFrontWaf(cf) => {
                let store = AwsSecretStore::new(sdk.clone());
                let edge = self.edge(&stacks, &store);
                match create {
                    true => edge.waf_create(ctx, &cf.name, sink, self.reload()).await,
                    false => edge.waf_update(ctx, &cf.name, sink, self.reload()).await,
                }
            }
            Resource::CloudFrontKeys(cf) => {
                let store = AwsSecretStore::new(sdk.clone());
                let edge = self.edge(&stacks, &store);
                match create {
                    true => edge.keys_create(ctx, &cf.name, sink, self.reload()).await,
                    false => edge.keys_update(ctx, &cf.name, sink, self.reload()).await,
                }
            }
            Resource::InboundDomain(domain) => {
                let aws = AwsInboundDomain { sdk };
                let deploy = parts::inbound_domain_deploy(&stacks, &aws);
                match create {
                    true => deploy.create(domain).await,
                    false => deploy.update(domain).await,
                }
            }
            Resource::S3(s3) => {
                let client = aws_sdk_s3::Client::new(&regional_config(sdk, &s3.region));
                let writer = BucketWriter {
                    client: &client,
                    s3,
                    cloudfront: &ctx.cloudfront,
                    retry: CreateRetry::default(),
                };
                match create {
                    true => writer.create(sink).await,
                    false => writer.update(sink).await,
                }
            }
            Resource::Vpc(vpc) => {
                let (efs, network) = (AwsEfs { sdk }, AwsVpcNetwork { sdk });
                let deploy = parts::vpc_deploy(&stacks, &efs, &network);
                match create {
                    true => deploy.create(vpc).await,
                    false => deploy.update(vpc).await,
                }
            }
            Resource::Dsql(context) => {
                if !create {
                    return no_update();
                }
                let (api, values) = (dsql::AwsDsql { sdk }, AwsStoredValues { sdk });
                parts::dsql_deploy(&api, &values)
                    .create(context, sink)
                    .await
            }
            Resource::Rds(rds) => {
                let (aws, stacks, values) = parts::rds_env(sdk, &rds.region);
                let deploy = parts::rds_deploy(&aws, &stacks, &values);
                match create {
                    true => deploy.create(rds, sink).await,
                    false => deploy.update(rds, sink).await,
                }
            }
            // backup は状態が常に COMPLETED で、deploy 後の確認が毎回適用する
            Resource::Backup(_) => Ok(()),
            Resource::Container(c) => {
                let env = parts::Env::new(sdk, ctx, &self.toml_dir).await?;
                let aws = env.aws();
                let deploy = env.deploy(&aws);
                match create {
                    true => deploy.create(ctx, &c.name, sink, self.reload()).await,
                    false => deploy.update(ctx, &c.name, sink, self.reload()).await,
                }
            }
            Resource::Inbound(inlet) => {
                let aws = AwsInbound { sdk };
                let deploy = parts::inbound_deploy(&stacks, &aws);
                match create {
                    true => deploy.create(inlet).await,
                    false => deploy.update(inlet).await,
                }
            }
            Resource::CloudFront(cf) => {
                let (stacks, aws, store) = parts::cloudfront_env(sdk);
                parts::cloudfront_deploy(&stacks, &aws, &store)
                    .update(ctx, &cf.name, sink)
                    .await
            }
        }
    }
}

/// deploy のときに出す DEPLOY_HASH の解決結果 (Python 版 `deploy_hash_report`)。
///
/// DEPLOY_HASH の環境変数が伝わらずに git の short hash へ落ちると、版や CloudFront の
/// cache の分離が効かない。使う値と出所を deploy のたびに見せる。container も
/// deploy_hash の route も無い構成では `None`
pub fn deploy_hash_report(ctx: &Context, env_set: bool) -> Option<String> {
    let from_cloudfront = ctx
        .cloudfront
        .values()
        .map(|cf| cf.deploy_hash.as_str())
        .find(|hash| !hash.is_empty());
    let from_container = || {
        ctx.container
            .values()
            .filter_map(|c| c.envs.get("DEPLOY_HASH"))
            .map(String::as_str)
            .find(|hash| !hash.is_empty())
    };
    let value = from_cloudfront.or_else(from_container)?;
    Some(if env_set {
        format!("DEPLOY_HASH = {value} (DEPLOY_HASH env より)")
    } else if value == "unknown" {
        "DEPLOY_HASH = unknown (git HEAD 取得失敗 かつ DEPLOY_HASH env 未設定)".to_string()
    } else {
        format!(
            "DEPLOY_HASH = {value} (git HEAD より; DEPLOY_HASH env は未設定 — \
             連続 deploy で版 / CloudFront cache を分けたい場合は \
             DEPLOY_HASH を明示してください)"
        )
    })
}

/// AWS の ECR に対する [`migrations::Repositories`]
pub struct AwsRepositories<'a> {
    pub sdk: &'a SdkConfig,
}

impl migrations::Repositories for AwsRepositories<'_> {
    async fn exists(&self, region: &str, name: &str) -> Result<bool> {
        let client = aws_sdk_ecr::Client::new(&regional_config(self.sdk, region));
        Ok(super::ecr::repository_uri(&client, name).await?.is_some())
    }

    async fn delete(&self, region: &str, name: &str) -> Result<()> {
        let client = aws_sdk_ecr::Client::new(&regional_config(self.sdk, region));
        super::ecr::delete_repository(&client, name)
            .await
            .with_context(|| format!("ECR repository {name} を削除できません"))?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stack::testing::vector_context;
    use crate::stack::StackOutputs;

    #[test]
    fn test_deploy_hash_report() {
        let ctx = vector_context("example/example-dsql.sandbox");
        let value = ctx
            .container
            .values()
            .find_map(|c| c.envs.get("DEPLOY_HASH").cloned())
            .unwrap();
        assert_eq!(
            deploy_hash_report(&ctx, true).unwrap(),
            format!("DEPLOY_HASH = {value} (DEPLOY_HASH env より)")
        );
        let from_git = deploy_hash_report(&ctx, false).unwrap();
        assert!(
            from_git.starts_with(&format!("DEPLOY_HASH = {value} (git HEAD より; "))
                || from_git.starts_with("DEPLOY_HASH = unknown (git HEAD 取得失敗"),
            "{from_git}"
        );
    }

    #[test]
    fn test_container_endpoints_prefer_domain_then_stack_output() {
        let ctx = vector_context("example/example-dsql.sandbox");
        let c = ctx.container.values().next().unwrap();
        let with_api: Vec<&String> = c
            .handlers
            .iter()
            .filter(|(_, h)| h.apigateway.is_some())
            .map(|(key, _)| key)
            .collect();
        assert!(!with_api.is_empty());
        // stack が未作成 (output が無い) で独自ドメインも無ければ、endpoint は無い
        let none = container_endpoints(c, &StackOutputs::new());
        assert!(none.iter().all(|(_, url)| url.starts_with("https://")));
        let key = with_api[0];
        let mut chars = key.chars();
        let capitalized = format!(
            "{}{}",
            chars.next().unwrap().to_uppercase(),
            chars.as_str().to_lowercase()
        );
        let outputs = StackOutputs::from([(
            format!("{capitalized}ApiEndpoint"),
            "https://abc.execute-api.example".to_string(),
        )]);
        let endpoints = container_endpoints(c, &outputs);
        let found = endpoints.iter().find(|(k, _)| k == key).unwrap();
        let api = c.handlers.get(key).unwrap().apigateway.as_ref().unwrap();
        match api.domain.as_deref() {
            Some(domain) => assert_eq!(found.1, format!("https://{domain}")),
            None => assert_eq!(found.1, "https://abc.execute-api.example"),
        }
    }
}
