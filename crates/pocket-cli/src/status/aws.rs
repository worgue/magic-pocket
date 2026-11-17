//! AWS / HTTP から今の状態を取り、status を決める ([`Evaluator`] の本番実装)。
//!
//! 呼ぶ API はすべて読み取り。Python 版との違い:
//! - 同じ stack を何度も describe しない (resource ごとに 1 回。結果は使い回す)
//! - 削除予約中の pocket managed secret を復元しない (無いものとして扱う)
//! - 「存在しない」に当たる error だけを NOEXIST にする (権限不足・throttling は止める)
//! - stack が COMPLETED のときだけ、template の描画に要る外部の値を取りに行く

use std::collections::{BTreeMap, HashMap};

use anyhow::{anyhow, bail, Result};
use aws_config::{Region, SdkConfig};
use base64::Engine as _;
use magic_pocket_settings::context::{
    CloudFrontContext, ContainerContext, Context, DsqlContext, HostedZone, InboundContext,
    InboundDomainContext, RdsContext, S3Context, SecretsContext, VpcContext,
};
use magic_pocket_settings::model::{PasswordStrategy, StoreType};
use serde_json::{Map, Value as Json};

use super::alerts::{self, Subscription};
use super::container::{self as container_status, HandlerState};
use super::inbound::{self as inbound_rules, ActiveRuleSet, ReceiptRule};
use super::providers::{self, Http};
use super::rds::{self as rds_status, ClusterPhase, ClusterState};
use super::report::{Evaluated, Evaluator};
use super::resource::Resource;
use super::sink::Sink;
use super::{dsql as dsql_status, s3 as s3_status};
use crate::aws_util::{has_code, sdk_error};
use crate::deploy::secret_store::{read_managed, ManagedSecrets};
use crate::stack::cloudfront::{self, CloudFrontInputs};
use crate::stack::container::{self, ContainerInputs, RdsState};
use crate::stack::ops::{
    cfn_status, stack_outputs, stack_status, CfnClient, ResourceStatus, StackApi, StackDescription,
    StackOutput,
};
use crate::stack::{inbound, vpc, StackOutputs, StackTemplate};

/// origin verify の secret を入れる managed secret の key (Python の `ORIGIN_VERIFY_SECRET_KEY`)
const ORIGIN_VERIFY_SECRET_KEY: &str = "POCKET_ORIGIN_VERIFY_SECRET";
const EDGE_REGION: &str = "us-east-1";

pub struct AwsEvaluator<'a> {
    ctx: &'a Context,
    sdk: SdkConfig,
    http: Option<Http>,
    /// (region, stack 名) → describe の結果
    stacks: HashMap<(String, String), Option<StackDescription>>,
    /// pocket_key → managed secret (store に無ければ `None`)
    secrets: HashMap<String, Option<ManagedSecrets>>,
    hosted_zones: Option<Vec<HostedZone>>,
}

impl<'a> AwsEvaluator<'a> {
    pub fn new(ctx: &'a Context, sdk: SdkConfig) -> Self {
        Self {
            ctx,
            sdk,
            http: None,
            stacks: HashMap::new(),
            secrets: HashMap::new(),
            hosted_zones: None,
        }
    }

    fn config(&self, region: &str) -> SdkConfig {
        self.sdk
            .to_builder()
            .region(Region::new(region.to_string()))
            .build()
    }

    fn http(&mut self) -> Result<&Http> {
        if self.http.is_none() {
            self.http = Some(Http::new()?);
        }
        Ok(self.http.as_ref().expect("作成済み"))
    }

    // ---- stack ----

    async fn describe(&mut self, region: &str, name: &str) -> Result<Option<StackDescription>> {
        let key = (region.to_string(), name.to_string());
        if let Some(found) = self.stacks.get(&key) {
            return Ok(found.clone());
        }
        let description = CfnClient::new(&self.sdk, region).describe(name).await?;
        self.stacks.insert(key, description.clone());
        Ok(description)
    }

    async fn outputs(&mut self, region: &str, name: &str) -> Result<Option<StackOutputs>> {
        let description = self.describe(region, name).await?;
        Ok(stack_outputs(description.as_ref()))
    }

    // ---- pocket managed secret ----

    /// store view の managed secret を読む (読むだけ。無ければ `None`)
    async fn managed_secrets(&mut self, view: &SecretsContext) -> Result<Option<ManagedSecrets>> {
        if let Some(found) = self.secrets.get(&view.pocket_key) {
            return Ok(found.clone());
        }
        let secrets = read_managed(&self.sdk, view, false).await?;
        self.secrets
            .insert(view.pocket_key.clone(), secrets.clone());
        Ok(secrets)
    }

    /// managed secret の key を持つ store view (shared → 名前順の container)。
    /// Python の `Context.managed_secret_view`
    fn managed_secret_view(&self, key: &str) -> Option<&'a SecretsContext> {
        if let Some(shared) = self.ctx.secrets.as_ref() {
            if shared.managed.contains_key(key) {
                return Some(shared);
            }
        }
        let mut names: Vec<&String> = self.ctx.container.keys().collect();
        names.sort();
        names.into_iter().find_map(|name| {
            self.ctx
                .container
                .get(name)
                .and_then(|c| c.secrets.as_ref())
                .filter(|view| view.managed.contains_key(key))
        })
    }

    // ---- stack 系の resource ----

    async fn acm(&mut self, cf: &CloudFrontContext) -> Result<ResourceStatus> {
        let name = cloudfront::acm_stack_name(cf);
        let description = self.describe(EDGE_REGION, &name).await?;
        if cfn_status(description.as_ref())? != ResourceStatus::Completed {
            return cfn_status(description.as_ref());
        }
        stack_status(description.as_ref(), &cloudfront::acm_stack(cf)?)
    }

    async fn waf(&mut self, cf: &CloudFrontContext, sink: &mut dyn Sink) -> Result<ResourceStatus> {
        let name = cloudfront::waf_stack_name(cf);
        let description = self.describe(EDGE_REGION, &name).await?;
        if cfn_status(description.as_ref())? != ResourceStatus::Completed {
            return cfn_status(description.as_ref());
        }
        let keys: &[String] = cf
            .waf
            .as_ref()
            .map(|waf| waf.header_secret_keys.as_slice())
            .unwrap_or_default();
        let mut values = BTreeMap::new();
        if !keys.is_empty() {
            match self.ctx.secrets.as_ref() {
                None => sink.warning("container secrets is not configured."),
                Some(view) => {
                    let secrets = self.managed_secrets(view).await?.unwrap_or_default();
                    for key in keys {
                        if let Some(value) = secrets.get(key).and_then(Json::as_str) {
                            values.insert(key.clone(), value.to_string());
                        }
                    }
                }
            }
        }
        stack_status(description.as_ref(), &cloudfront::waf_stack(cf, &values)?)
    }

    async fn keys(
        &mut self,
        cf: &CloudFrontContext,
        sink: &mut dyn Sink,
    ) -> Result<ResourceStatus> {
        let name = cloudfront::keys_stack_name(cf);
        let description = self.describe(&cf.region, &name).await?;
        if cfn_status(description.as_ref())? != ResourceStatus::Completed {
            return cfn_status(description.as_ref());
        }
        let pem = self.signing_public_key_pem(cf, sink).await?;
        stack_status(description.as_ref(), &cloudfront::keys_stack(cf, &pem)?)
    }

    /// managed secret から署名用の公開鍵 (PEM) を読む。無ければ空文字
    async fn signing_public_key_pem(
        &mut self,
        cf: &CloudFrontContext,
        sink: &mut dyn Sink,
    ) -> Result<String> {
        let Some(key) = cf.signing_key.as_deref().filter(|k| !k.is_empty()) else {
            return Ok(String::new());
        };
        let Some(view) = self.managed_secret_view(key) else {
            sink.warning("container secrets is not configured.");
            return Ok(String::new());
        };
        let secrets = self.managed_secrets(view).await?.unwrap_or_default();
        let Some(data) = secrets.get(key) else {
            sink.warning(&format!(
                "signing_key '{key}' not found in managed secrets. \
                 Deploy the container first to generate the key."
            ));
            return Ok(String::new());
        };
        let Some(public) = data.get("pub").and_then(Json::as_str) else {
            return Ok(String::new());
        };
        let decoded = base64::engine::general_purpose::STANDARD
            .decode(public)
            .map_err(|e| anyhow!("signing_key '{key}' の公開鍵を base64 として読めません: {e}"))?;
        Ok(String::from_utf8(decoded)?)
    }

    async fn cloudfront(
        &mut self,
        cf: &CloudFrontContext,
        sink: &mut dyn Sink,
    ) -> Result<ResourceStatus> {
        let name = cloudfront::cloudfront_stack_name(cf);
        let description = self.describe(&cf.region, &name).await?;
        if cfn_status(description.as_ref())? != ResourceStatus::Completed {
            return cfn_status(description.as_ref());
        }
        self.warn_kvs_secrets(cf, sink).await?;
        let origin_verify_secret = self.origin_verify_secret(cf, sink).await?;
        let acm_output = match cf.domain.as_deref().filter(|d| !d.is_empty()) {
            Some(_) => {
                self.outputs(EDGE_REGION, &cloudfront::acm_stack_name(cf))
                    .await?
            }
            None => None,
        };
        let waf_output = match cf.waf.as_ref() {
            Some(_) => {
                self.outputs(EDGE_REGION, &cloudfront::waf_stack_name(cf))
                    .await?
            }
            None => None,
        };
        let inputs = CloudFrontInputs {
            acm_output,
            waf_output,
            origin_verify_secret,
        };
        stack_status(
            description.as_ref(),
            &cloudfront::cloudfront_stack(cf, &inputs)?,
        )
    }

    /// token_secret / basic_auth は template には入らない (deploy 後に KVS へ書く) が、
    /// store に無い・形が違うときの警告は Python 版と同じく status でも出す
    async fn warn_kvs_secrets(
        &mut self,
        cf: &CloudFrontContext,
        sink: &mut dyn Sink,
    ) -> Result<()> {
        if let Some(key) = cf.token_secret.as_deref().filter(|k| !k.is_empty()) {
            if let Some(view) = self.managed_secret_view(key) {
                let secrets = self.managed_secrets(view).await?.unwrap_or_default();
                match secrets.get(key) {
                    Some(value) if value.is_string() => {}
                    Some(_) => {
                        sink.warning(&format!("token_secret の値が文字列ではありません: {key}"))
                    }
                    None => sink.warning(&format!(
                        "token_secret '{key}' が managed secrets に見つかりません"
                    )),
                }
            }
        }
        if let Some(key) = cf.basic_auth.as_deref().filter(|k| !k.is_empty()) {
            if let Some(view) = self.managed_secret_view(key) {
                let secrets = self.managed_secrets(view).await?.unwrap_or_default();
                let valid = secrets
                    .get(key)
                    .and_then(Json::as_str)
                    .is_some_and(|value| value.contains(':'));
                if !valid {
                    sink.warning(&format!(
                        "basic_auth '{key}' の値が 'user:pass' 形式ではありません"
                    ));
                }
            }
        }
        Ok(())
    }

    async fn origin_verify_secret(
        &mut self,
        cf: &CloudFrontContext,
        sink: &mut dyn Sink,
    ) -> Result<String> {
        if !cf.enable_origin_verify {
            return Ok(String::new());
        }
        let Some(view) = self.ctx.secrets.as_ref() else {
            return Ok(String::new());
        };
        let secrets = self.managed_secrets(view).await?.unwrap_or_default();
        match secrets.get(ORIGIN_VERIFY_SECRET_KEY).and_then(Json::as_str) {
            Some(value) => Ok(value.to_string()),
            None => {
                sink.warning(&format!(
                    "origin verify secret '{ORIGIN_VERIFY_SECRET_KEY}' が managed secrets に見つかりません"
                ));
                Ok(String::new())
            }
        }
    }

    async fn vpc(&mut self, v: &VpcContext) -> Result<ResourceStatus> {
        let name = vpc::stack_name(v);
        let description = self.describe(&v.region, &name).await?;
        if cfn_status(description.as_ref())? != ResourceStatus::Completed {
            return cfn_status(description.as_ref());
        }
        let efs_id = match v.efs.as_ref() {
            Some(efs) => self.efs_filesystem_id(&efs.region, &efs.name).await?,
            None => None,
        };
        stack_status(description.as_ref(), &vpc::vpc_stack(v, efs_id.as_deref())?)
    }

    /// Name タグが一致する EFS の id
    async fn efs_filesystem_id(&self, region: &str, name: &str) -> Result<Option<String>> {
        let client = aws_sdk_efs::Client::new(&self.config(region));
        let response = client
            .describe_file_systems()
            .send()
            .await
            .map_err(|e| sdk_error("efs describe_file_systems", &e))?;
        if response.next_marker().is_some() {
            bail!("Your efs number is over 100. Please implement here.");
        }
        Ok(response
            .file_systems()
            .iter()
            .find(|fs| {
                fs.tags()
                    .iter()
                    .any(|tag| tag.key() == "Name" && tag.value() == name)
            })
            .map(|fs| fs.file_system_id().to_string()))
    }

    async fn inbound_domain(
        &mut self,
        domain: &InboundDomainContext,
        sink: &mut dyn Sink,
    ) -> Result<ResourceStatus> {
        let description = self.describe(&domain.region, &domain.stack_name).await?;
        if cfn_status(description.as_ref())? != ResourceStatus::Completed {
            return cfn_status(description.as_ref());
        }
        let zone = if domain.manage_dns {
            Some(
                self.hosted_zone_id(
                    &domain.domain,
                    domain.hosted_zone_id_override.as_deref(),
                    sink,
                )
                .await?,
            )
        } else {
            None
        };
        let stack = inbound::inbound_domain_stack(domain, zone.as_deref());
        stack_status(description.as_ref(), &stack)
    }

    /// domain の hosted zone の id (override があればそれ。無ければ Route53 から探す)
    async fn hosted_zone_id(
        &mut self,
        domain: &str,
        override_id: Option<&str>,
        sink: &mut dyn Sink,
    ) -> Result<String> {
        if let Some(id) = override_id.filter(|id| !id.is_empty()) {
            return Ok(id.to_string());
        }
        sink.log(&format!("Searching hostedzone_id from domain: {domain}"));
        if self.hosted_zones.is_none() {
            let client = aws_sdk_route53::Client::new(&self.sdk);
            let response = client
                .list_hosted_zones()
                .send()
                .await
                .map_err(|e| sdk_error("route53 list_hosted_zones", &e))?;
            if response.is_truncated() {
                bail!("Route53 hosted zone list is truncated. Please set hosted_zone_id.");
            }
            self.hosted_zones = Some(
                response
                    .hosted_zones()
                    .iter()
                    .map(|zone| HostedZone {
                        name: zone.name().to_string(),
                        id: zone.id().to_string(),
                    })
                    .collect(),
            );
        }
        let zones = self.hosted_zones.as_deref().unwrap_or_default();
        let Some(zone) = find_hosted_zone(zones, domain) else {
            bail!(
                "No route53 hosted zone for the domain. [{domain}]\n\
                 Check your route53 hosted zone or set hosted_zone_id in pocket.toml"
            );
        };
        let id = zone.id.get("/hostedzone/".len()..).unwrap_or_default();
        sink.log("Found hostedzone");
        sink.log(&format!("  Name: {}", zone.name));
        sink.log(&format!("  Id: {id}"));
        Ok(id.to_string())
    }

    async fn inbound(&mut self, inlet: &InboundContext) -> Result<ResourceStatus> {
        let description = self.describe(&inlet.region, &inlet.resource_name).await?;
        if cfn_status(description.as_ref())? != ResourceStatus::Completed {
            return cfn_status(description.as_ref());
        }
        let active = self.active_rule_set(&inlet.region).await?;
        let outputs = stack_outputs(description.as_ref());
        let (rule_set, after) = inbound_rules::resolve_rules(&active, inlet, outputs.as_ref())?;
        let stack = inbound::inbound_stack(inlet, &rule_set, after.as_deref());
        stack_status(description.as_ref(), &stack)
    }

    async fn active_rule_set(&self, region: &str) -> Result<ActiveRuleSet> {
        let client = aws_sdk_ses::Client::new(&self.config(region));
        let response = client
            .describe_active_receipt_rule_set()
            .send()
            .await
            .map_err(|e| sdk_error("ses describe_active_receipt_rule_set", &e))?;
        Ok(ActiveRuleSet {
            name: response
                .metadata()
                .and_then(|metadata| metadata.name())
                .map(str::to_string),
            rules: response
                .rules()
                .iter()
                .map(|rule| ReceiptRule {
                    name: rule.name().to_string(),
                    enabled: rule.enabled(),
                    recipients: rule.recipients().to_vec(),
                    stopping: rule.actions().iter().any(|action| {
                        action.stop_action().is_some()
                            || action.bounce_action().is_some()
                            || action.lambda_action().is_some()
                    }),
                })
                .collect(),
        })
    }

    // ---- container ----

    async fn container(&mut self, c: &ContainerContext) -> Result<ResourceStatus> {
        let lambda = aws_sdk_lambda::Client::new(&self.config(&c.region));
        let mut handlers = Vec::new();
        for handler in c.handlers.values() {
            handlers.push(handler_state(&lambda, &handler.function_name).await?);
        }
        let mut handler_statuses = Vec::new();
        for handler in &handlers {
            handler_statuses.push(container_status::handler_status(handler)?);
        }
        let name = container::stack_name(c);
        let description = self.describe(&c.region, &name).await?;
        let mut stack = cfn_status(description.as_ref())?;
        if stack == ResourceStatus::Completed {
            let template = self.container_template(c).await?;
            stack = stack_status(description.as_ref(), &template)?;
        }
        if let Some(status) = container_status::status_before_image(&handler_statuses, stack) {
            return Ok(status);
        }
        let image_hash = self.image_hash(c).await?;
        Ok(container_status::image_status(
            &handlers,
            image_hash.as_deref(),
        ))
    }

    async fn container_template(&mut self, c: &ContainerContext) -> Result<StackTemplate> {
        let rds = self.ctx.rds.as_ref().filter(|_| c.vpc.is_some());
        let rds_state = match rds.filter(|rds| rds.managed) {
            Some(rds) => self.rds_state_for_container(rds).await?,
            None => RdsState::default(),
        };
        let dsql_identifier = match self.ctx.dsql.as_ref() {
            Some(dsql) => self
                .dsql_cluster(dsql)
                .await?
                .map(|(identifier, _)| identifier),
            None => None,
        };
        let vpc_output = match c.vpc.as_ref().filter(|v| !v.manage) {
            Some(v) => self.outputs(&v.region, &vpc::stack_name(v)).await?,
            None => None,
        };
        let inputs = ContainerInputs {
            rds: rds_state,
            dsql_identifier,
            vpc_output,
        };
        container::container_stack(
            c,
            self.ctx.rds.as_ref(),
            self.ctx.dsql.as_ref(),
            self.ctx.scheduler.get(&c.name),
            &inputs,
        )
    }

    /// ECR の `:<stage>` tag の image の digest (repository か image が無ければ `None`)
    async fn image_hash(&self, c: &ContainerContext) -> Result<Option<String>> {
        let client = aws_sdk_ecr::Client::new(&self.config(&c.region));
        let response = client
            .describe_images()
            .repository_name(&c.ecr_name)
            .image_ids(
                aws_sdk_ecr::types::ImageIdentifier::builder()
                    .image_tag(&c.stage)
                    .build(),
            )
            .send()
            .await;
        let response = match response {
            Ok(response) => response,
            Err(e)
                if has_code(
                    &e,
                    &["RepositoryNotFoundException", "ImageNotFoundException"],
                ) =>
            {
                return Ok(None)
            }
            Err(e) => return Err(sdk_error("ecr describe_images", &e)),
        };
        let digest = response
            .image_details()
            .first()
            .and_then(|detail| detail.image_digest());
        container_status::image_hash(digest)
    }

    // ---- RDS ----

    async fn rds_cluster(&self, rds: &RdsContext) -> Result<Option<ClusterState>> {
        let client = aws_sdk_rds::Client::new(&self.config(&rds.region));
        let response = client
            .describe_db_clusters()
            .db_cluster_identifier(&rds.cluster_identifier)
            .send()
            .await;
        let response = match response {
            Ok(response) => response,
            Err(e) if has_code(&e, &["DBClusterNotFoundFault"]) => return Ok(None),
            Err(e) => return Err(sdk_error("rds describe_db_clusters", &e)),
        };
        Ok(response.db_clusters().first().map(|cluster| {
            let scaling = cluster.serverless_v2_scaling_configuration();
            let secret = cluster.master_user_secret();
            ClusterState {
                status: cluster.status().unwrap_or_default().to_string(),
                min_capacity: scaling.and_then(|s| s.min_capacity()),
                max_capacity: scaling.and_then(|s| s.max_capacity()),
                backup_retention_period: cluster.backup_retention_period().map(i64::from),
                has_master_user_secret: secret.is_some(),
                master_user_secret_arn: secret.and_then(|s| s.secret_arn()).map(str::to_string),
                endpoint: cluster.endpoint().map(str::to_string),
                port: cluster.port().map(i64::from),
            }
        }))
    }

    /// static のパスワードを入れた secret (Secrets Manager) の ARN。無ければ `None`
    async fn static_secret_arn(&self, rds: &RdsContext) -> Result<Option<String>> {
        let client = aws_sdk_secretsmanager::Client::new(&self.config(&rds.region));
        let response = client
            .describe_secret()
            .secret_id(&rds.credentials_secret_name)
            .send()
            .await;
        match response {
            Ok(response) => Ok(response.arn().map(str::to_string)),
            Err(e) if has_code(&e, &["ResourceNotFoundException"]) => Ok(None),
            Err(e) => Err(sdk_error("secretsmanager describe_secret", &e)),
        }
    }

    /// 接続情報の secret の ARN (Python の `Rds.master_user_secret_arn`)
    async fn master_user_secret_arn(
        &self,
        rds: &RdsContext,
        cluster: Option<&ClusterState>,
    ) -> Result<Option<String>> {
        if rds.password_strategy == PasswordStrategy::Static {
            // secret_store = ssm のときは Secrets Manager に secret を作らない
            if rds.secret_store != StoreType::Sm {
                return Ok(None);
            }
            return self.static_secret_arn(rds).await;
        }
        Ok(cluster.and_then(|c| c.master_user_secret_arn.clone()))
    }

    async fn rds_state_for_container(&self, rds: &RdsContext) -> Result<RdsState> {
        let ec2 = aws_sdk_ec2::Client::new(&self.config(&rds.region));
        let groups = ec2
            .describe_security_groups()
            .filters(
                aws_sdk_ec2::types::Filter::builder()
                    .name("tag:Name")
                    .values(&rds.security_group_name)
                    .build(),
            )
            .send()
            .await
            .map_err(|e| sdk_error("ec2 describe_security_groups", &e))?;
        let cluster = self.rds_cluster(rds).await?;
        let is_static = rds.password_strategy == PasswordStrategy::Static;
        let static_secret_arn = if is_static && rds.secret_store == StoreType::Sm {
            self.static_secret_arn(rds).await?
        } else {
            None
        };
        Ok(RdsState {
            security_group_id: groups
                .security_groups()
                .first()
                .and_then(|group| group.group_id())
                .map(str::to_string),
            endpoint: cluster.as_ref().and_then(|c| c.endpoint.clone()),
            port: cluster.as_ref().and_then(|c| c.port),
            master_user_secret_arn: cluster.and_then(|c| c.master_user_secret_arn),
            static_secret_arn,
        })
    }

    async fn rds(&self, rds: &RdsContext) -> Result<ResourceStatus> {
        if !rds.managed {
            return Ok(ResourceStatus::Completed);
        }
        let Some(cluster) = self.rds_cluster(rds).await? else {
            return Ok(ResourceStatus::NoExist);
        };
        match rds_status::cluster_phase(&cluster.status) {
            ClusterPhase::Failed => return Ok(ResourceStatus::Failed),
            ClusterPhase::Progress => return Ok(ResourceStatus::Progress),
            ClusterPhase::Available => {}
        }
        if !rds_status::scaling_matches(rds, &cluster)
            || !rds_status::backup_retention_matches(rds, &cluster)
            || !self.password_state_matches(rds, &cluster).await?
            || !self.rotation_matches(rds, &cluster).await?
        {
            return Ok(ResourceStatus::RequireUpdate);
        }
        Ok(ResourceStatus::Completed)
    }

    /// パスワードの管理方式 (AWS 管理か static か) と、static のときの置き場が望みどおりか
    async fn password_state_matches(
        &self,
        rds: &RdsContext,
        cluster: &ClusterState,
    ) -> Result<bool> {
        if rds.password_strategy == PasswordStrategy::AwsManaged {
            return Ok(cluster.has_master_user_secret);
        }
        if cluster.has_master_user_secret {
            return Ok(false);
        }
        if rds.secret_store == StoreType::Ssm {
            let client = aws_sdk_ssm::Client::new(&self.config(&rds.region));
            let response = client
                .get_parameter()
                .name(&rds.credentials_secret_name)
                .send()
                .await;
            return match response {
                Ok(_) => Ok(true),
                Err(e) if has_code(&e, &["ParameterNotFound"]) => Ok(false),
                Err(e) => Err(sdk_error("ssm get_parameter", &e)),
            };
        }
        Ok(self.static_secret_arn(rds).await?.is_some())
    }

    async fn rotation_matches(&self, rds: &RdsContext, cluster: &ClusterState) -> Result<bool> {
        let Some(desired) = rds_status::desired_rotation(rds) else {
            return Ok(true);
        };
        let Some(arn) = self.master_user_secret_arn(rds, Some(cluster)).await? else {
            return Ok(true);
        };
        let client = aws_sdk_secretsmanager::Client::new(&self.config(&rds.region));
        let response = client
            .describe_secret()
            .secret_id(&arn)
            .send()
            .await
            .map_err(|e| sdk_error("secretsmanager describe_secret", &e))?;
        let mut current = Map::new();
        if let Some(rules) = response.rotation_rules() {
            if let Some(expression) = rules.schedule_expression() {
                current.insert("ScheduleExpression".to_string(), Json::from(expression));
            }
            if let Some(duration) = rules.duration() {
                current.insert("Duration".to_string(), Json::from(duration));
            }
        }
        Ok(rds_status::rotation_matches(&desired, &current))
    }

    // ---- DSQL ----

    /// Name タグが一致する最初の cluster の (identifier, 状態)。全ページを辿る
    async fn dsql_cluster(&self, dsql: &DsqlContext) -> Result<Option<(String, String)>> {
        let client = aws_sdk_dsql::Client::new(&self.config(&dsql.region));
        let mut pages = client.list_clusters().into_paginator().send();
        while let Some(page) = pages.next().await {
            let page = page.map_err(|e| sdk_error("dsql list_clusters", &e))?;
            for summary in page.clusters() {
                let identifier = summary.identifier();
                // 一覧の後で消えた cluster は飛ばす (他の error は止める)
                let detail = match client.get_cluster().identifier(identifier).send().await {
                    Ok(detail) => detail,
                    Err(e) if has_code(&e, &["ResourceNotFoundException"]) => continue,
                    Err(e) => return Err(sdk_error("dsql get_cluster", &e)),
                };
                let tags = client
                    .list_tags_for_resource()
                    .resource_arn(detail.arn())
                    .send()
                    .await;
                let tags = match tags {
                    Ok(tags) => tags,
                    Err(e) if has_code(&e, &["ResourceNotFoundException"]) => continue,
                    Err(e) => return Err(sdk_error("dsql list_tags_for_resource", &e)),
                };
                let name = tags.tags().and_then(|tags| tags.get("Name"));
                if name.map(String::as_str) == Some(dsql.tag_name.as_str()) {
                    return Ok(Some((
                        detail.identifier().to_string(),
                        detail.status().as_str().to_string(),
                    )));
                }
            }
        }
        Ok(None)
    }

    // ---- S3 ----

    async fn s3(&self, s3: &S3Context, sink: &mut dyn Sink) -> Result<ResourceStatus> {
        let client = aws_sdk_s3::Client::new(&self.config(&s3.region));
        let bucket = s3.bucket_name.as_str();
        match client.head_bucket().bucket(bucket).send().await {
            Ok(_) => {}
            Err(e) if e.raw_response().is_some_and(|r| r.status().as_u16() == 404) => {
                return Ok(ResourceStatus::NoExist)
            }
            // 404 以外の応答 (403 など) は、他の account が同じ名前を使っている見込み
            Err(e) if e.raw_response().is_some() => {
                bail!("Bucket might be already used by other account. Try another bucket_prefix.")
            }
            Err(e) => return Err(sdk_error("s3 head_bucket", &e)),
        }
        let block = match client.get_public_access_block().bucket(bucket).send().await {
            Ok(response) => response.public_access_block_configuration,
            Err(e) if has_code(&e, &["NoSuchPublicAccessBlockConfiguration"]) => None,
            Err(e) => return Err(sdk_error("s3 get_public_access_block", &e)),
        };
        let blocked = block.is_some_and(|b| {
            b.block_public_acls == Some(true)
                && b.ignore_public_acls == Some(true)
                && b.block_public_policy == Some(true)
                && b.restrict_public_buckets == Some(true)
        });
        if !blocked {
            return Ok(ResourceStatus::RequireUpdate);
        }
        let desired_cors = s3_status::desired_cors(s3, &self.ctx.cloudfront, sink);
        let current_cors = match client.get_bucket_cors().bucket(bucket).send().await {
            Ok(response) => Some(s3_status::cors_rules_json(response.cors_rules())),
            Err(e) if has_code(&e, &["NoSuchCORSConfiguration"]) => None,
            Err(e) => return Err(sdk_error("s3 get_bucket_cors", &e)),
        };
        if desired_cors != current_cors {
            return Ok(ResourceStatus::RequireUpdate);
        }
        let versioning = client
            .get_bucket_versioning()
            .bucket(bucket)
            .send()
            .await
            .map_err(|e| sdk_error("s3 get_bucket_versioning", &e))?;
        let current = versioning.status().map(|status| status.as_str());
        if !s3_status::versioning_matches(s3, current) {
            return Ok(ResourceStatus::RequireUpdate);
        }
        let desired_lifecycle = s3_status::desired_lifecycle(s3);
        let current_lifecycle = match client
            .get_bucket_lifecycle_configuration()
            .bucket(bucket)
            .send()
            .await
        {
            Ok(response) => Some(s3_status::lifecycle_rules_json(response.rules())),
            Err(e) if has_code(&e, &["NoSuchLifecycleConfiguration"]) => None,
            Err(e) => return Err(sdk_error("s3 get_bucket_lifecycle_configuration", &e)),
        };
        if desired_lifecycle != current_lifecycle {
            return Ok(ResourceStatus::RequireUpdate);
        }
        Ok(ResourceStatus::Completed)
    }

    // ---- 末尾の情報 ----

    async fn alert_lines(&self, sink: &mut dyn Sink) -> Result<()> {
        let targets = alerts::targets(self.ctx);
        if targets.is_empty() {
            return Ok(());
        }
        let identity = aws_sdk_sts::Client::new(&self.sdk)
            .get_caller_identity()
            .send()
            .await
            .map_err(|e| sdk_error("sts get_caller_identity", &e))?;
        let account = identity.account().unwrap_or_default();
        for target in targets {
            let client = aws_sdk_sns::Client::new(&self.config(&target.region));
            let topic_arn = format!(
                "arn:aws:sns:{}:{account}:{}",
                target.region, target.topic_name
            );
            // Python 版と同じく先頭のページ (100 件) だけを見る
            let response = client
                .list_subscriptions_by_topic()
                .topic_arn(&topic_arn)
                .send()
                .await;
            let state = match response {
                Ok(response) => {
                    let subscriptions: Vec<Subscription> = response
                        .subscriptions()
                        .iter()
                        .map(|s| Subscription {
                            protocol: s.protocol().unwrap_or_default().to_string(),
                            endpoint: s.endpoint().unwrap_or_default().to_string(),
                            arn: s.subscription_arn().unwrap_or_default().to_string(),
                        })
                        .collect();
                    alerts::subscription_state(&subscriptions, &target.email)
                }
                Err(e) if has_code(&e, &["NotFound", "NotFoundException"]) => "NotCreated",
                Err(e) => return Err(sdk_error("sns list_subscriptions_by_topic", &e)),
            };
            let line = format!(
                "DLQ alert {} -> {}: {state}",
                target.topic_name, target.email
            );
            if state == "Confirmed" {
                sink.success(&line);
            } else {
                sink.warning(&line);
            }
        }
        Ok(())
    }

    async fn inbound_details(&mut self, sink: &mut dyn Sink) -> Result<()> {
        let ctx = self.ctx;
        for (name, inlet) in ctx.inbound.iter() {
            let description = self.describe(&inlet.region, &inlet.resource_name).await?;
            let Some(output) = stack_outputs(description.as_ref()) else {
                continue;
            };
            let value = |key: &str| {
                output.get(key).ok_or_else(|| {
                    anyhow!(
                        "inbound stack {} の output に {key} がありません",
                        inlet.resource_name
                    )
                })
            };
            sink.print(&format!(
                "inbound.{name}: rule set={} / bucket={}",
                value("RuleSet")?,
                value("Bucket")?
            ));
            let outputs = description.as_ref().map(|d| d.outputs.as_slice());
            for line in inbound_alarm_lines(outputs.unwrap_or_default()) {
                sink.print(&line);
            }
            let alert = &inlet.config.delivery_alert;
            if alert.enabled && alert.eventbridge {
                sink.print(
                    "  alarm作成済み。EventBridgeから外部通知先への接続・着信は未検証です。",
                );
            }
        }
        Ok(())
    }
}

/// inbound stack の output のうち alarm の ARN の行 (stack の Outputs の順)
fn inbound_alarm_lines(outputs: &[StackOutput]) -> Vec<String> {
    let mut lines = Vec::new();
    for output in outputs {
        // Python 版の output は OutputKey と Export 名の両方を key に持つ
        for key in [Some(&output.key), output.export_name.as_ref()] {
            if let Some(key) = key.filter(|key| key.ends_with("AlarmArn")) {
                lines.push(format!("  {key}: {}", output.value));
            }
        }
    }
    lines
}

/// domain を含む hosted zone のうち、名前が最も長いもの
fn find_hosted_zone<'z>(zones: &'z [HostedZone], domain: &str) -> Option<&'z HostedZone> {
    let mut matched: Vec<&HostedZone> = zones
        .iter()
        .filter(|zone| {
            let name = zone.name.trim_matches('.');
            domain == name || domain.ends_with(&format!(".{name}"))
        })
        .collect();
    matched.sort_by_key(|zone| std::cmp::Reverse(zone.name.chars().count()));
    matched.first().copied()
}

/// `get_function` の結果。function が無ければ空 (NOEXIST)
async fn handler_state(
    client: &aws_sdk_lambda::Client,
    function_name: &str,
) -> Result<HandlerState> {
    let response = client
        .get_function()
        .function_name(function_name)
        .send()
        .await;
    let response = match response {
        Ok(response) => response,
        Err(e) if has_code(&e, &["ResourceNotFoundException"]) => {
            return Ok(HandlerState::default())
        }
        Err(e) => return Err(sdk_error("lambda get_function", &e)),
    };
    let configuration = response.configuration();
    Ok(HandlerState {
        last_update_status: configuration
            .and_then(|c| c.last_update_status())
            .map(|status| status.as_str().to_string()),
        code_sha256: configuration
            .and_then(|c| c.code_sha256())
            .map(str::to_string),
    })
}

impl Evaluator for AwsEvaluator<'_> {
    async fn status(&mut self, resource: &Resource<'_>, sink: &mut dyn Sink) -> Result<Evaluated> {
        let status = match resource {
            Resource::CloudFrontAcm(cf) => self.acm(cf).await?,
            Resource::CloudFrontWaf(cf) => self.waf(cf, sink).await?,
            Resource::InboundDomain(domain) => self.inbound_domain(domain, sink).await?,
            Resource::Neon(neon) => {
                // provider の資格情報が無い環境でも、stack の状態確認は続ける
                if neon.api_key.as_deref().is_none_or(str::is_empty) {
                    return Ok(Evaluated::Skip(
                        "Neon status: NEON_API_KEY が未設定のため状態確認をスキップします \
                         (stack の状態確認・削除に Neon の資格情報は不要です)"
                            .to_string(),
                    ));
                }
                self.http()?;
                let http = self.http.as_ref().expect("作成済み");
                providers::neon_status(http, neon, sink).await?
            }
            Resource::TiDb(tidb) => providers::tidb_status(self.http()?, tidb).await?,
            Resource::Upstash(upstash) => providers::upstash_status(self.http()?, upstash).await?,
            Resource::S3(s3) => self.s3(s3, sink).await?,
            Resource::Vpc(v) => self.vpc(v).await?,
            Resource::Dsql(dsql) => {
                let cluster = self.dsql_cluster(dsql).await?;
                dsql_status::cluster_status(
                    cluster
                        .as_ref()
                        .map(|(id, status)| (id.as_str(), status.as_str())),
                )?
            }
            Resource::Rds(rds) => self.rds(rds).await?,
            // backup は deploy のたびに冪等に適用するので、常に COMPLETED
            Resource::Backup(_) => ResourceStatus::Completed,
            Resource::CloudFrontKeys(cf) => self.keys(cf, sink).await?,
            Resource::Container(c) => self.container(c).await?,
            Resource::Inbound(inlet) => self.inbound(inlet).await?,
            Resource::CloudFront(cf) => self.cloudfront(cf, sink).await?,
        };
        Ok(Evaluated::Status(status))
    }

    async fn secrets_not_ready(&mut self, container: &ContainerContext) -> Result<usize> {
        let mut count = 0;
        for view in container.secrets_views() {
            // SSM の store は「まだ無い」状態を持たない (path の ARN を組むだけ)
            if view.managed.is_empty() || view.store != StoreType::Sm {
                continue;
            }
            if self.managed_secrets(view).await?.is_none() {
                count += 1;
            }
        }
        Ok(count)
    }

    async fn tail(&mut self, sink: &mut dyn Sink) -> Result<()> {
        self.alert_lines(sink).await?;
        self.inbound_details(sink).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn zone(name: &str, id: &str) -> HostedZone {
        HostedZone {
            name: name.to_string(),
            id: format!("/hostedzone/{id}"),
        }
    }

    #[test]
    fn test_find_hosted_zone_prefers_longest_match() {
        let zones = [
            zone("example.com.", "A"),
            zone("mail.example.com.", "B"),
            zone("other.net.", "C"),
        ];
        assert_eq!(
            find_hosted_zone(&zones, "in.mail.example.com").unwrap().id,
            "/hostedzone/B"
        );
        assert_eq!(
            find_hosted_zone(&zones, "example.com").unwrap().id,
            "/hostedzone/A"
        );
        assert!(find_hosted_zone(&zones, "notexample.com").is_none());
    }

    #[test]
    fn test_inbound_alarm_lines_keep_output_order() {
        let output = |key: &str, value: &str| StackOutput {
            key: key.to_string(),
            value: value.to_string(),
            export_name: None,
        };
        let outputs = [
            output("Bucket", "b"),
            output("QueueAgeAlarmArn", "arn:2"),
            output("DeliveryDLQAlarmArn", "arn:1"),
            output("RuleSet", "r"),
        ];
        assert_eq!(
            inbound_alarm_lines(&outputs),
            ["  QueueAgeAlarmArn: arn:2", "  DeliveryDLQAlarmArn: arn:1"]
        );
    }
}
