//! `pocket/general_settings.py` の `GeneralSettings` と、`[iam]` (`Iam`)。

use serde::{Deserialize, Serialize};

use crate::error::{child, PathSeg, SettingsError};
use crate::model::django::Django;
use crate::types::{EfsAccessPointPath, EfsMountPath, ZoneSuffix};

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct GeneralSettings {
    #[serde(default = "default_namespace")]
    pub namespace: String,
    #[serde(default = "default_prefix_template")]
    pub prefix_template: String,
    pub region: String,
    /// 省略時は読み込み時に `LoadContext::project_name_fallback` で補う
    pub project_name: String,
    pub stages: Vec<String>,
    #[serde(default)]
    pub s3_fallback_bucket_name: Option<String>,
    #[serde(default)]
    pub django_fallback: Django,
}

fn default_namespace() -> String {
    "pocket".to_string()
}

fn default_prefix_template() -> String {
    "{stage}-{project}-{namespace}-".to_string()
}

impl GeneralSettings {
    pub(crate) fn validate(&mut self, path: &[PathSeg]) -> Result<(), SettingsError> {
        self.django_fallback
            .validate(&child(path, "django_fallback"))
    }
}

#[derive(Debug, Clone, PartialEq, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Iam {
    /// true なら pocket は IAM role を作らず、既存の role を名前で参照する
    #[serde(default)]
    pub external_roles: bool,
    /// external_roles の role 名に使う namespace。既定は `[general] namespace`
    #[serde(default)]
    pub namespace: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Efs {
    #[serde(default = "default_local_mount_path")]
    pub local_mount_path: EfsMountPath,
    #[serde(default = "default_access_point_path")]
    pub access_point_path: EfsAccessPointPath,
}

fn default_local_mount_path() -> EfsMountPath {
    EfsMountPath("/mnt/efs".to_string())
}

fn default_access_point_path() -> EfsAccessPointPath {
    EfsAccessPointPath("/lambda".to_string())
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Vpc {
    pub r#ref: String,
    #[serde(default)]
    pub zone_suffixes: Vec<ZoneSuffix>,
    #[serde(default = "default_true")]
    pub nat_gateway: bool,
    #[serde(default = "default_true")]
    pub internet_gateway: bool,
    #[serde(default)]
    pub efs: Option<Efs>,
    #[serde(default = "default_true")]
    pub manage: bool,
    #[serde(default)]
    pub sharable: bool,
}

pub(crate) fn default_true() -> bool {
    true
}

impl Vpc {
    pub(crate) fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        let err = |m: &str| Err(SettingsError::value(path.to_vec(), m));
        // check_managed
        if self.manage {
            if self.zone_suffixes.is_empty() {
                return err("zone_suffixes is required when manage=true");
            }
            if self.nat_gateway && !self.internet_gateway {
                return err("nat_gateway without internet_gateway is not supported.");
            }
            if self.internet_gateway && !self.nat_gateway {
                return err(
                    "lambda runs in private subnet, internet_gateway without nat_gateway \
                     is not supported yet.\nWe should support it in the future if we want \
                     to support fargate.",
                );
            }
        }
        // check_unmanaged
        if !self.manage {
            if self.sharable {
                return err("sharable requires manage=true");
            }
            if self.efs.is_some() {
                return err("efs requires manage=true");
            }
        }
        Ok(())
    }
}
