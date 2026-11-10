//! `[dsql]` / `[rds]` / `[backup]` (Python の `Dsql` / `Rds` / `RdsBackup` / `Backup` /
//! `DsqlBackupPlan` / `RdsBackupPlan` と backup rule 6 種)。

use serde::{Deserialize, Serialize};

use crate::error::{child, PathSeg, SettingsError};
use crate::model::general::Vpc;
use crate::types::{RotationDuration, RotationSchedule};

#[derive(Debug, Clone, PartialEq, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Dsql {
    #[serde(default)]
    pub deletion_protection: bool,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RdsBackup {
    #[serde(
        default = "default_retention_days",
        deserialize_with = "crate::types::retention_1_35"
    )]
    pub retention_days: i64,
}

impl Default for RdsBackup {
    fn default() -> Self {
        Self {
            retention_days: default_retention_days(),
        }
    }
}

fn default_retention_days() -> i64 {
    35
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize, Serialize)]
pub enum PasswordStrategy {
    #[default]
    #[serde(rename = "aws-managed")]
    AwsManaged,
    #[serde(rename = "static")]
    Static,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Rds {
    #[serde(default = "crate::model::general::default_true")]
    pub managed: bool,
    #[serde(default)]
    pub backup: RdsBackup,
    /// 前処理 (use_vpc の解決) で決まる
    #[serde(default)]
    pub vpc: Option<Vpc>,
    #[serde(default = "default_min_capacity")]
    pub min_capacity: f64,
    #[serde(default = "default_max_capacity")]
    pub max_capacity: f64,
    #[serde(default)]
    pub snapshot_identifier: Option<String>,
    #[serde(default)]
    pub database: Option<String>,
    #[serde(default)]
    pub password_strategy: PasswordStrategy,
    #[serde(default)]
    pub rotation_schedule: Option<RotationSchedule>,
    #[serde(default)]
    pub rotation_duration: Option<RotationDuration>,
    #[serde(default)]
    pub secret_arn: Option<String>,
    #[serde(default)]
    pub security_group_id: Option<String>,
}

fn default_min_capacity() -> f64 {
    0.5
}
fn default_max_capacity() -> f64 {
    2.0
}

impl Rds {
    pub(crate) fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        if let Some(vpc) = &self.vpc {
            vpc.validate(&child(path, "vpc"))?;
        }
        let err = |m: String| Err(SettingsError::value(path.to_vec(), m));
        // check_managed_mode
        let unmanaged: Vec<&str> = [
            ("secret_arn", self.secret_arn.is_some()),
            ("security_group_id", self.security_group_id.is_some()),
        ]
        .iter()
        .filter(|(_, set)| *set)
        .map(|(k, _)| *k)
        .collect();
        let managed_custom: Vec<&str> = [
            ("min_capacity (非デフォルト)", self.min_capacity != 0.5),
            ("max_capacity (非デフォルト)", self.max_capacity != 2.0),
            ("snapshot_identifier", self.snapshot_identifier.is_some()),
            ("database", self.database.is_some()),
            ("backup (非デフォルト)", self.backup != RdsBackup::default()),
            ("rotation_schedule", self.rotation_schedule.is_some()),
            ("rotation_duration", self.rotation_duration.is_some()),
        ]
        .iter()
        .filter(|(_, set)| *set)
        .map(|(k, _)| *k)
        .collect();
        if self.rotation_duration.is_some() && self.rotation_schedule.is_none() {
            return err("rotation_duration は rotation_schedule と併せて指定します。".to_string());
        }
        if self.managed
            && self.rotation_schedule.is_some()
            && self.password_strategy != PasswordStrategy::AwsManaged
        {
            return err(
                "rotation_schedule は password_strategy = \"aws-managed\" でのみ\
                 使用できます。static はローテーションしません。"
                    .to_string(),
            );
        }
        if self.managed && !unmanaged.is_empty() {
            return err(format!(
                "{} は managed = false でのみ使用できます。 \
                 既存の RDS を参照する場合は managed = false を指定してください。",
                unmanaged.join(", ")
            ));
        }
        if !self.managed {
            if self.secret_arn.is_none() {
                return err("managed = false の場合、secret_arn は必須です。".to_string());
            }
            if self.security_group_id.is_none() {
                return err("managed = false の場合、security_group_id は必須です。".to_string());
            }
            if self.password_strategy != PasswordStrategy::AwsManaged {
                return err("password_strategy は managed = true でのみ使用できます。 \
                     既存 RDS 参照時のパスワードは secret_arn の secret に従います。"
                    .to_string());
            }
            if !managed_custom.is_empty() {
                return err(format!(
                    "managed = false では {} は 使用できません。",
                    managed_custom.join(", ")
                ));
            }
        }
        Ok(())
    }
}

/// backup rule (GFS の 1 階層)。dsql の rule だけが cold storage を持つ
/// (Aurora の snapshot は cold storage 非対応のため rds の rule には書けない)
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DsqlDailyRule {
    #[serde(default = "dsql_daily_cron")]
    pub cron: String,
    #[serde(default = "dsql_daily_delete", deserialize_with = "crate::types::ge1")]
    pub delete_after_days: i64,
    #[serde(default = "dsql_daily_cold", deserialize_with = "crate::types::ge0")]
    pub cold_storage_after_days: i64,
}

fn dsql_daily_cron() -> String {
    "0 3 * * ? *".to_string()
}
fn dsql_daily_delete() -> i64 {
    35
}
fn dsql_daily_cold() -> i64 {
    0
}

impl Default for DsqlDailyRule {
    fn default() -> Self {
        Self {
            cron: dsql_daily_cron(),
            delete_after_days: dsql_daily_delete(),
            cold_storage_after_days: dsql_daily_cold(),
        }
    }
}

impl DsqlDailyRule {
    fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        check_lifecycle(self.delete_after_days, self.cold_storage_after_days, path)
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DsqlWeeklyRule {
    #[serde(default = "dsql_weekly_cron")]
    pub cron: String,
    #[serde(default = "dsql_weekly_delete", deserialize_with = "crate::types::ge1")]
    pub delete_after_days: i64,
    #[serde(default = "dsql_weekly_cold", deserialize_with = "crate::types::ge0")]
    pub cold_storage_after_days: i64,
}

fn dsql_weekly_cron() -> String {
    "0 4 ? * 1 *".to_string()
}
fn dsql_weekly_delete() -> i64 {
    365
}
fn dsql_weekly_cold() -> i64 {
    90
}

impl Default for DsqlWeeklyRule {
    fn default() -> Self {
        Self {
            cron: dsql_weekly_cron(),
            delete_after_days: dsql_weekly_delete(),
            cold_storage_after_days: dsql_weekly_cold(),
        }
    }
}

impl DsqlWeeklyRule {
    fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        check_lifecycle(self.delete_after_days, self.cold_storage_after_days, path)
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DsqlMonthlyRule {
    #[serde(default = "dsql_monthly_cron")]
    pub cron: String,
    #[serde(
        default = "dsql_monthly_delete",
        deserialize_with = "crate::types::ge1"
    )]
    pub delete_after_days: i64,
    #[serde(default = "dsql_monthly_cold", deserialize_with = "crate::types::ge0")]
    pub cold_storage_after_days: i64,
}

fn dsql_monthly_cron() -> String {
    "0 5 1 * ? *".to_string()
}
fn dsql_monthly_delete() -> i64 {
    1095
}
fn dsql_monthly_cold() -> i64 {
    90
}

impl Default for DsqlMonthlyRule {
    fn default() -> Self {
        Self {
            cron: dsql_monthly_cron(),
            delete_after_days: dsql_monthly_delete(),
            cold_storage_after_days: dsql_monthly_cold(),
        }
    }
}

impl DsqlMonthlyRule {
    fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        check_lifecycle(self.delete_after_days, self.cold_storage_after_days, path)
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RdsWeeklyRule {
    #[serde(default = "rds_weekly_cron")]
    pub cron: String,
    #[serde(default = "rds_weekly_delete", deserialize_with = "crate::types::ge1")]
    pub delete_after_days: i64,
}

fn rds_weekly_cron() -> String {
    "0 4 ? * 1 *".to_string()
}
fn rds_weekly_delete() -> i64 {
    365
}

impl Default for RdsWeeklyRule {
    fn default() -> Self {
        Self {
            cron: rds_weekly_cron(),
            delete_after_days: rds_weekly_delete(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RdsMonthlyRule {
    #[serde(default = "rds_monthly_cron")]
    pub cron: String,
    #[serde(default = "rds_monthly_delete", deserialize_with = "crate::types::ge1")]
    pub delete_after_days: i64,
}

fn rds_monthly_cron() -> String {
    "0 5 1 * ? *".to_string()
}
fn rds_monthly_delete() -> i64 {
    1095
}

impl Default for RdsMonthlyRule {
    fn default() -> Self {
        Self {
            cron: rds_monthly_cron(),
            delete_after_days: rds_monthly_delete(),
        }
    }
}

/// DsqlBackupRule.check_lifecycle (cold storage は移してから 90 日以上置く)
fn check_lifecycle(delete: i64, cold: i64, path: &[PathSeg]) -> Result<(), SettingsError> {
    if cold != 0 {
        let minimum = cold + 90;
        if delete < minimum {
            return Err(SettingsError::value(
                path.to_vec(),
                format!(
                    "delete_after_days は cold_storage_after_days + 90 以上に してください \
                     (AWS Backup の cold storage 最低保持期間)。 cold_storage_after_days = \
                     {cold} なら {minimum} 以上が必要です。"
                ),
            ));
        }
    }
    Ok(())
}

/// `[backup.dsql]` (DSQL は PITR を持たないので daily 階層から持つ)
#[derive(Debug, Clone, PartialEq, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DsqlBackupPlan {
    #[serde(default)]
    pub daily: DsqlDailyRule,
    #[serde(default)]
    pub weekly: DsqlWeeklyRule,
    #[serde(default)]
    pub monthly: DsqlMonthlyRule,
}

impl DsqlBackupPlan {
    fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        self.daily.validate(&child(path, "daily"))?;
        self.weekly.validate(&child(path, "weekly"))?;
        self.monthly.validate(&child(path, "monthly"))
    }
}

/// `[backup.rds]` (直近は PITR が担うので daily 階層は持たない)
#[derive(Debug, Clone, PartialEq, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RdsBackupPlan {
    #[serde(default)]
    pub weekly: RdsWeeklyRule,
    #[serde(default)]
    pub monthly: RdsMonthlyRule,
}

/// DB 層の定期バックアップ (AWS Backup の backup plan)
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Backup {
    #[serde(default)]
    pub deletable: bool,
    #[serde(default = "default_timezone")]
    pub timezone: String,
    #[serde(default)]
    pub dsql: Option<DsqlBackupPlan>,
    #[serde(default)]
    pub rds: Option<RdsBackupPlan>,
}

fn default_timezone() -> String {
    "UTC".to_string()
}

impl Backup {
    pub(crate) fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        if let Some(dsql) = &self.dsql {
            dsql.validate(&child(path, "dsql"))?;
        }
        // check_has_target
        if self.dsql.is_none() && self.rds.is_none() {
            return Err(SettingsError::value(
                path.to_vec(),
                "[backup] には [backup.dsql] / [backup.rds] の少なくとも 一方の宣言が必要です。",
            ));
        }
        Ok(())
    }
}
