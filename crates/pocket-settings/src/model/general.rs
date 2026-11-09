//! `pocket/general_settings.py` の `GeneralSettings` と、`[iam]` (`Iam`)。

use serde::{Deserialize, Serialize};

use crate::error::{child, PathSeg, SettingsError};
use crate::model::django::Django;

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
