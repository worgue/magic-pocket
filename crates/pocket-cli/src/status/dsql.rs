//! Aurora DSQL cluster の状態 (Python 版 `resources/dsql.py` の `status`)。

use anyhow::{bail, Result};

use crate::stack::ops::ResourceStatus;

/// cluster の状態名から status を決める。cluster が無ければ NOEXIST
pub fn cluster_status(cluster: Option<(&str, &str)>) -> Result<ResourceStatus> {
    let Some((identifier, status)) = cluster else {
        return Ok(ResourceStatus::NoExist);
    };
    Ok(match status {
        "DELETED" => ResourceStatus::NoExist,
        "CREATING" | "UPDATING" | "DELETING" | "PENDING_SETUP" | "PENDING_DELETE" => {
            ResourceStatus::Progress
        }
        "ACTIVE" | "IDLE" => ResourceStatus::Completed,
        "FAILED" | "INACTIVE" => ResourceStatus::Failed,
        _ => bail!("DSQL cluster {identifier} が未知の状態 ({status}) です。"),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_cluster_status_table() {
        // Python 版 tests/test_dsql.py と同じ表
        use ResourceStatus::*;
        assert_eq!(cluster_status(None).unwrap(), NoExist);
        for (status, want) in [
            ("ACTIVE", Completed),
            ("IDLE", Completed),
            ("CREATING", Progress),
            ("UPDATING", Progress),
            ("DELETING", Progress),
            ("PENDING_SETUP", Progress),
            ("PENDING_DELETE", Progress),
            ("DELETED", NoExist),
            ("FAILED", Failed),
            ("INACTIVE", Failed),
        ] {
            assert_eq!(
                cluster_status(Some(("abc", status))).unwrap(),
                want,
                "{status}"
            );
        }
        let error = cluster_status(Some(("abc", "MYSTERY"))).unwrap_err();
        assert_eq!(
            error.to_string(),
            "DSQL cluster abc が未知の状態 (MYSTERY) です。"
        );
    }
}
