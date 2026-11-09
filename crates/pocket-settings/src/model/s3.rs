//! `[s3]` (Python の `S3` / `S3Cors` / `S3LifecycleRule`)。

use serde::{Deserialize, Serialize};

use crate::error::{child, PathSeg, SettingsError};
use crate::types::FormatStr;

/// 文字列 1 つか文字列の配列 (Python の `str | list[str]`)
#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(untagged)]
pub enum OneOrMany {
    One(String),
    Many(Vec<String>),
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct S3Cors {
    pub methods: Vec<String>,
    pub cloudfront: OneOrMany,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct S3LifecycleRule {
    pub id: String,
    pub prefix: String,
    #[serde(default, deserialize_with = "crate::types::opt_ge1")]
    pub expiration_days: Option<i64>,
    #[serde(default, deserialize_with = "crate::types::opt_ge1")]
    pub noncurrent_version_expiration_days: Option<i64>,
}

impl S3LifecycleRule {
    fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        if self.expiration_days.is_none() && self.noncurrent_version_expiration_days.is_none() {
            return Err(SettingsError::value(
                path.to_vec(),
                "expiration_days か noncurrent_version_expiration_days の\
                 少なくとも一方を指定してください。",
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct S3 {
    #[serde(default = "default_bucket_name_format")]
    pub bucket_name_format: FormatStr,
    #[serde(default)]
    pub cors: Option<S3Cors>,
    #[serde(default)]
    pub versioning: bool,
    #[serde(default)]
    pub lifecycle_rules: Vec<S3LifecycleRule>,
}

fn default_bucket_name_format() -> FormatStr {
    FormatStr("{stage}-{project}-{namespace}".to_string())
}

impl S3 {
    pub(crate) fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        let rules = child(path, "lifecycle_rules");
        for (i, rule) in self.lifecycle_rules.iter().enumerate() {
            let mut p = rules.clone();
            p.push(PathSeg::Index(i));
            rule.validate(&p)?;
        }
        Ok(())
    }
}
