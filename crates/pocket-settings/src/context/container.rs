//! `ContainerContext` とその部品 (`pocket/context.py` の前半)

use serde::Serialize;

use super::general::{DjangoContext, InboundContext, InboundRuntimeConfig, VpcContext};
use crate::model::{BuildBackend, ManagedSecretSpec, Platform, StoreType, UserSecretSpec};
use crate::types::{AnyDict, OrderedMap};

#[derive(Debug, Clone, Serialize)]
pub struct ApiGatewayContext {
    pub domain: Option<String>,
    pub create_records: bool,
    pub hosted_zone_id_override: Option<String>,
    // computed
    pub disable_execute_api_endpoint: bool,
    pub hosted_zone_id: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SqsDeadLetterAlertContext {
    pub email: Option<String>,
    pub eventbridge: bool,
    pub topic_name: String,
    pub alarm_name: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct SqsContext {
    pub retain: bool,
    pub batch_size: i64,
    pub message_retention_period: i64,
    pub maximum_concurrency: i64,
    pub dead_letter_max_receive_count: i64,
    pub dead_letter_message_retention_period: i64,
    pub report_batch_item_failures: bool,
    pub name: String,
    pub visibility_timeout: i64,
    pub dead_letter_alert: Option<SqsDeadLetterAlertContext>,
}

#[derive(Debug, Clone, Serialize)]
pub struct LambdaHandlerContext {
    pub command: String,
    pub timeout: i64,
    pub memory_size: i64,
    pub reserved_concurrency: Option<i64>,
    pub envs: OrderedMap<String>,
    pub region: String,
    pub apigateway: Option<ApiGatewayContext>,
    pub sqs: Option<SqsContext>,
    pub key: String,
    pub function_name: String,
    pub log_group_name: String,
    pub export_api_domain: Option<String>,
    // computed
    pub cloudformation_cert_ref_name: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct SecretsContext {
    pub store: StoreType,
    pub managed: OrderedMap<ManagedSecretSpec>,
    pub user: OrderedMap<UserSecretSpec>,
    pub extra_resources: Vec<String>,
    pub require_list_secrets: bool,
    pub region: String,
    pub pocket_key: String,
    pub stage: String,
    pub project_name: String,
    // computed
    pub allowed_sm_resources: Vec<String>,
    pub allowed_ssm_resources: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct BuildContext {
    pub backend: BuildBackend,
    pub compute_type: String,
    pub depot_project_id: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ContainerIamContext {
    pub managed_policy_arns: Vec<String>,
    pub inline_policies: OrderedMap<AnyDict>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ContainerContext {
    pub name: String,
    pub vpc: Option<VpcContext>,
    /// container store (`{stage}-{project}-{name}-{namespace}`) の view
    pub secrets: Option<SecretsContext>,
    /// shared store (`{stage}-{project}-{namespace}`) の per-container view
    pub shared_secrets: Option<SecretsContext>,
    pub dockerfile_path: String,
    pub envs: OrderedMap<String>,
    pub signing_key_imports: OrderedMap<String>,
    pub inbound: OrderedMap<InboundContext>,
    pub platform: Platform,
    pub django: Option<DjangoContext>,
    pub region: String,
    pub slug: String,
    pub stage: String,
    pub namespace: String,
    pub resource_prefix: String,
    pub handlers: OrderedMap<LambdaHandlerContext>,
    pub ecr_name: String,
    pub ecr_name_overridden: bool,
    /// promote 時に CLI が設定する実行時フラグ (toml からは読まない)
    pub promote_commit_hash: Option<String>,
    pub use_s3: bool,
    pub use_ses: bool,
    pub use_route53: bool,
    pub use_sqs: bool,
    pub use_efs: bool,
    pub permissions_boundary: Option<String>,
    pub iam: ContainerIamContext,
    pub external_roles: bool,
    pub external_role_prefix: String,
    pub efs_local_mount_path: String,
    pub build: BuildContext,
    // computed ([`Self::refresh_computed`] で確定する)
    pub lambda_architecture: String,
    pub allowed_sm_resources: Vec<String>,
    pub allowed_ssm_resources: Vec<String>,
    pub require_list_secrets: bool,
    pub inbound_runtime_config: OrderedMap<InboundRuntimeConfig>,
}

impl ContainerContext {
    /// この container が読む store view (container store → shared store)
    pub fn secrets_views(&self) -> Vec<&SecretsContext> {
        [&self.secrets, &self.shared_secrets]
            .into_iter()
            .flatten()
            .collect()
    }

    /// computed field を今の field から計算し直す (inbound の接続後に呼ぶ)
    pub(crate) fn refresh_computed(&mut self) {
        self.lambda_architecture = match self.platform {
            Platform::LinuxArm64 => "arm64",
            Platform::LinuxAmd64 => "x86_64",
        }
        .to_string();
        let mut sm: Vec<String> = Vec::new();
        let mut ssm: Vec<String> = Vec::new();
        for sc in self.secrets_views() {
            for arn in &sc.allowed_sm_resources {
                if !sm.contains(arn) {
                    sm.push(arn.clone());
                }
            }
            for arn in &sc.allowed_ssm_resources {
                if !ssm.contains(arn) {
                    ssm.push(arn.clone());
                }
            }
        }
        self.require_list_secrets = self
            .secrets_views()
            .iter()
            .any(|sc| sc.require_list_secrets);
        self.allowed_sm_resources = sm;
        self.allowed_ssm_resources = ssm;
        let mut runtime = OrderedMap::default();
        for (name, inlet) in self.inbound.iter() {
            runtime.insert(name.clone(), inlet.runtime_config.clone());
        }
        self.inbound_runtime_config = runtime;
    }
}
