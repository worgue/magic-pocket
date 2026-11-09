//! 設定モデル本体。Python の `pocket.settings.Settings` と同じ形・同じ検証。

pub mod django;
pub mod general;

use serde::{Deserialize, Serialize};

use crate::error::{path, SettingsError};
use crate::types::TagStr;

pub use django::{CacheStore, Django, DjangoCache, DjangoStorage, PublishMode, StorageStore};
pub use general::{GeneralSettings, Iam};

/// pocket.toml の top-level に書ける key (Python の `Settings.model_fields` から
/// `stage` を除いたもの)。stage 上書きの table と区別する前処理で使う
pub(crate) const TOP_LEVEL_KEYS: &[&str] = &[
    "general",
    "vpc",
    "container",
    "neon",
    "tidb",
    "upstash",
    "dsql",
    "rds",
    "backup",
    "ses",
    "inbound",
    "s3",
    "cloudfront",
    "scheduler",
    "iam",
];

/// まだ Rust に移していない top-level の section と、宣言が無いときの JSON 値が
/// `{}` (dict の section) か `null` か。移した section から順に外す (移行中だけの表)
const UNPORTED_SECTIONS: &[(&str, bool)] = &[
    ("vpc", false),
    ("container", true),
    ("neon", false),
    ("tidb", false),
    ("upstash", false),
    ("dsql", false),
    ("rds", false),
    ("backup", false),
    ("ses", false),
    ("inbound", true),
    ("s3", false),
    ("cloudfront", true),
    ("scheduler", false),
];

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Settings {
    pub general: GeneralSettings,
    pub stage: TagStr,
    #[serde(default)]
    pub iam: Iam,
}

impl Settings {
    pub fn project_name(&self) -> &str {
        &self.general.project_name
    }

    /// 環境の識別子。e.g) dev-myprj (Python の computed field `slug`)
    pub fn slug(&self) -> String {
        format!("{}-{}", self.stage.as_str(), self.general.project_name)
    }

    /// Python の `model_dump(mode="json")` と同じ形の JSON
    /// (全 field を出し、`None` は `null`、computed field も出す)
    pub fn to_json(&self) -> serde_json::Value {
        let mut value = serde_json::to_value(self).expect("Settings は JSON にできる");
        let obj = value.as_object_mut().expect("Settings は object");
        for (key, is_dict) in UNPORTED_SECTIONS {
            let empty = if *is_dict {
                serde_json::Value::Object(serde_json::Map::new())
            } else {
                serde_json::Value::Null
            };
            obj.insert((*key).to_string(), empty);
        }
        obj.insert("slug".to_string(), serde_json::Value::String(self.slug()));
        value
    }

    /// Python の validator を宣言順に (入れ子のモデルが先)
    pub(crate) fn validate(&mut self) -> Result<(), SettingsError> {
        self.general.validate(&path(&["general"]))?;
        Ok(())
    }
}
