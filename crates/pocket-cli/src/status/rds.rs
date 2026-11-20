//! RDS (Aurora) cluster の状態 (Python 版 `resources/rds.py` の `status`)。

use magic_pocket_settings::context::RdsContext;
use serde_json::{Map, Value as Json};

/// `describe_db_clusters` のうち判定に使う項目
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ClusterState {
    pub status: String,
    pub min_capacity: Option<f64>,
    pub max_capacity: Option<f64>,
    pub backup_retention_period: Option<i64>,
    /// AWS 管理のパスワード (`MasterUserSecret`) を持つか
    pub has_master_user_secret: bool,
    pub master_user_secret_arn: Option<String>,
    pub endpoint: Option<String>,
    pub port: Option<i64>,
    pub arn: Option<String>,
    pub engine_version: Option<String>,
}

/// `describe_db_clusters` の応答から判定に使う項目を取り出す
pub fn cluster_state(cluster: &aws_sdk_rds::types::DbCluster) -> ClusterState {
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
        arn: cluster.db_cluster_arn().map(str::to_string),
        engine_version: cluster.engine_version().map(str::to_string),
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClusterPhase {
    Available,
    Failed,
    /// 作成中・変更中など (上のどれでもない状態はすべて)
    Progress,
}

pub fn cluster_phase(status: &str) -> ClusterPhase {
    match status {
        "available" => ClusterPhase::Available,
        "failed" | "inaccessible-encryption-credentials" | "storage-full" | "stopped" => {
            ClusterPhase::Failed
        }
        _ => ClusterPhase::Progress,
    }
}

pub fn scaling_matches(rds: &RdsContext, cluster: &ClusterState) -> bool {
    cluster.min_capacity == Some(rds.min_capacity) && cluster.max_capacity == Some(rds.max_capacity)
}

pub fn backup_retention_matches(rds: &RdsContext, cluster: &ClusterState) -> bool {
    cluster.backup_retention_period == Some(rds.backup_retention_days)
}

/// 望む rotation の設定 (`RotationRules` と比べる項目)。rotation の宣言が無ければ `None`
pub fn desired_rotation(rds: &RdsContext) -> Option<Map<String, Json>> {
    let schedule = rds.rotation_schedule.as_ref()?;
    let mut desired = Map::new();
    desired.insert(
        "ScheduleExpression".to_string(),
        Json::from(schedule.as_str()),
    );
    if let Some(duration) = rds.rotation_duration.as_ref() {
        desired.insert("Duration".to_string(), Json::from(duration.as_str()));
    }
    Some(desired)
}

/// secret の `RotationRules` が望む設定を含むか (望む側の key だけ比べる)
pub fn rotation_matches(desired: &Map<String, Json>, current: &Map<String, Json>) -> bool {
    desired
        .iter()
        .all(|(key, value)| current.get(key) == Some(value))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stack::testing::vector_context;
    use serde_json::json;

    #[test]
    fn test_cluster_phase_table() {
        // Python 版 tests/test_status_classification.py と同じ表
        for status in [
            "creating",
            "modifying",
            "backing-up",
            "upgrading",
            "resetting-master-credentials",
        ] {
            assert_eq!(cluster_phase(status), ClusterPhase::Progress, "{status}");
        }
        for status in [
            "failed",
            "inaccessible-encryption-credentials",
            "storage-full",
            "stopped",
        ] {
            assert_eq!(cluster_phase(status), ClusterPhase::Failed, "{status}");
        }
        assert_eq!(cluster_phase("available"), ClusterPhase::Available);
    }

    #[test]
    fn test_scaling_and_retention_drift() {
        let ctx = vector_context("toml/rds.dev");
        let rds = ctx.rds.as_ref().unwrap();
        let mut cluster = ClusterState {
            min_capacity: Some(rds.min_capacity),
            max_capacity: Some(rds.max_capacity),
            backup_retention_period: Some(rds.backup_retention_days),
            ..Default::default()
        };
        assert!(scaling_matches(rds, &cluster));
        assert!(backup_retention_matches(rds, &cluster));
        cluster.max_capacity = Some(rds.max_capacity + 1.0);
        cluster.backup_retention_period = Some(rds.backup_retention_days + 1);
        assert!(!scaling_matches(rds, &cluster));
        assert!(!backup_retention_matches(rds, &cluster));
        // scaling の設定が無い cluster (provisioned) は不一致
        cluster.min_capacity = None;
        assert!(!scaling_matches(rds, &cluster));
    }

    #[test]
    fn test_rotation_compares_only_desired_keys() {
        let desired = json!({"ScheduleExpression": "rate(30 days)", "Duration": "3h"});
        let desired = desired.as_object().unwrap();
        let same = json!({
            "ScheduleExpression": "rate(30 days)",
            "Duration": "3h",
            "AutomaticallyAfterDays": 30
        });
        assert!(rotation_matches(desired, same.as_object().unwrap()));
        let other = json!({"ScheduleExpression": "rate(7 days)", "Duration": "3h"});
        assert!(!rotation_matches(desired, other.as_object().unwrap()));
        let missing = json!({"ScheduleExpression": "rate(30 days)"});
        assert!(!rotation_matches(desired, missing.as_object().unwrap()));
    }
}
