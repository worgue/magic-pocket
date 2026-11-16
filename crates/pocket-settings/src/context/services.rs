//! 外部サービス・DB・S3・scheduler の Context (`pocket/context.py` の中盤)

use serde::Serialize;

use super::general::VpcContext;
use crate::model::{PasswordStrategy, ProvisioningMode, StoreType};
use crate::types::{RotationDuration, RotationSchedule};

#[derive(Debug, Clone, Serialize)]
pub struct NeonContext {
    pub pg_version: i64,
    pub api_key: Option<String>,
    pub project_name: String,
    pub branch_name: String,
    /// branch_name が設定で明示されたか (未指定 = default branch)
    pub branch_name_specified: bool,
    pub stage: Option<String>,
    pub parent_branch_name: Option<String>,
    pub name: String,
    pub role_name: String,
    pub provisioning: ProvisioningMode,
}

#[derive(Debug, Clone, Serialize)]
pub struct TiDbContext {
    pub public_key: Option<String>,
    pub private_key: Option<String>,
    pub tidb_project: Option<String>,
    pub cluster_name: String,
    pub database_name: String,
    pub region: String,
    pub project_name: String,
    pub provisioning: ProvisioningMode,
}

#[derive(Debug, Clone, Serialize)]
pub struct UpstashContext {
    pub email: Option<String>,
    pub api_key: Option<String>,
    pub database_name: String,
    pub budget: i64,
    pub provisioning: ProvisioningMode,
}

/// backup plan の 1 rule (GFS の 1 階層)
#[derive(Debug, Clone, Serialize)]
pub struct BackupRuleContext {
    pub name: String,
    pub schedule_expression: String,
    pub delete_after_days: i64,
    pub cold_storage_after_days: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct BackupPlanContext {
    /// "dsql" | "rds"
    pub service: String,
    pub plan_name: String,
    pub rules: Vec<BackupRuleContext>,
}

#[derive(Debug, Clone, Serialize)]
pub struct BackupContext {
    pub region: String,
    pub vault_name: String,
    pub role_name: String,
    pub deletable: bool,
    pub timezone: String,
    pub plans: Vec<BackupPlanContext>,
    pub cleanup_plan_names: Vec<String>,
    pub legacy_plan_names: Vec<String>,
    pub permissions_boundary: Option<String>,
    pub external_roles: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct DsqlContext {
    pub region: String,
    pub tag_name: String,
    pub backup_vault_name: String,
    pub backup_role_name: String,
    pub deletion_protection: bool,
    pub backup: Option<BackupPlanContext>,
    pub endpoint_secret_name: String,
    pub endpoint_secret_store: StoreType,
    pub permissions_boundary: Option<String>,
    pub external_roles: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct RdsContext {
    pub managed: bool,
    pub vpc: Option<VpcContext>,
    pub min_capacity: f64,
    pub max_capacity: f64,
    pub snapshot_identifier: Option<String>,
    pub backup_retention_days: i64,
    pub region: String,
    pub cluster_identifier: String,
    pub instance_identifier: String,
    pub database_name: String,
    pub master_username: String,
    pub subnet_group_name: String,
    pub security_group_name: String,
    pub slug: String,
    pub password_strategy: PasswordStrategy,
    pub rotation_schedule: Option<RotationSchedule>,
    pub rotation_duration: Option<RotationDuration>,
    pub credentials_secret_name: String,
    pub secret_store: StoreType,
    pub secret_arn: Option<String>,
    pub security_group_id: Option<String>,
}

impl RdsContext {
    /// managed = false の既定値 (Python の field の既定)
    pub(crate) fn unmanaged(
        region: String,
        secret_arn: Option<String>,
        security_group_id: Option<String>,
    ) -> Self {
        Self {
            managed: false,
            vpc: None,
            min_capacity: 0.5,
            max_capacity: 2.0,
            snapshot_identifier: None,
            backup_retention_days: 35,
            region,
            cluster_identifier: String::new(),
            instance_identifier: String::new(),
            database_name: String::new(),
            master_username: "postgres".to_string(),
            subnet_group_name: String::new(),
            security_group_name: String::new(),
            slug: String::new(),
            password_strategy: PasswordStrategy::AwsManaged,
            rotation_schedule: None,
            rotation_duration: None,
            credentials_secret_name: String::new(),
            secret_store: StoreType::Sm,
            secret_arn,
            security_group_id,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct SesContext {
    pub from_email: String,
    pub region: String,
    pub configuration_set: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ScheduleEntryContext {
    pub key: String,
    pub scheduler: String,
    /// container ローカルの handler key
    pub handler: String,
    pub schedule_expression: String,
    pub name: String,
    pub yaml_key: String,
    pub input_json: String,
    // computed
    pub is_django_management: bool,
    pub is_sqs: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct SchedulerContext {
    pub schedules: Vec<ScheduleEntryContext>,
    pub role_name: String,
    pub invoked_function_arns: Vec<String>,
    pub sqs_queue_arns: Vec<String>,
    // computed
    pub has_schedules: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct S3CorsContext {
    pub methods: Vec<String>,
    pub cloudfront_names: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct S3LifecycleRuleContext {
    pub id: String,
    pub prefix: String,
    pub expiration_days: Option<i64>,
    pub noncurrent_version_expiration_days: Option<i64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct S3Context {
    pub region: String,
    pub bucket_name: String,
    pub cors: Option<S3CorsContext>,
    pub versioning: bool,
    pub lifecycle_rules: Vec<S3LifecycleRuleContext>,
}
