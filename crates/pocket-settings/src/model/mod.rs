//! 設定モデル本体。Python の `pocket.settings.Settings` と同じ形・同じ検証。

pub mod container;
pub mod django;
pub mod general;
pub mod s3;

use serde::{Deserialize, Serialize};

use crate::error::{path, SettingsError};
use std::sync::OnceLock;

use regex::Regex;

use crate::error::child;
use crate::types::{OrderedMap, TagStr};

pub use container::{
    Alert, ApiGateway, BuildBackend, BuildConfig, Container, ContainerIam, LambdaHandler,
    ManagedSecretSpec, ManagedSecretType, Platform, Secrets, Sqs, StoreType, UserSecretSpec,
    UserSecretType,
};
pub use django::{CacheStore, Django, DjangoCache, DjangoStorage, PublishMode, StorageStore};
pub use general::{Efs, GeneralSettings, Iam, Vpc};
pub use s3::{OneOrMany, S3Cors, S3LifecycleRule, S3};

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
    ("neon", false),
    ("tidb", false),
    ("upstash", false),
    ("dsql", false),
    ("rds", false),
    ("backup", false),
    ("ses", false),
    ("inbound", true),
    ("cloudfront", true),
    ("scheduler", false),
];

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Settings {
    pub general: GeneralSettings,
    pub stage: TagStr,
    #[serde(default)]
    pub vpc: Option<Vpc>,
    #[serde(default)]
    pub container: OrderedMap<Container>,
    #[serde(default)]
    pub s3: Option<S3>,
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
        if let Some(vpc) = &self.vpc {
            vpc.validate(&path(&["vpc"]))?;
        }
        let containers = path(&["container"]);
        for (name, c) in self.container.iter_mut() {
            c.validate(&child(&containers, name))?;
        }
        if let Some(s3) = &self.s3 {
            s3.validate(&path(&["s3"]))?;
        }
        self.check_container_names()?;
        self.check_container_secrets_consistency()?;
        Ok(())
    }

    fn check_container_names(&self) -> Result<(), SettingsError> {
        static RE: OnceLock<Regex> = OnceLock::new();
        let re = RE.get_or_init(|| Regex::new("^[a-z][a-z0-9]{0,31}$").expect("re"));
        for name in self.container.keys() {
            if !re.is_match(name) {
                return Err(SettingsError::value(
                    Vec::new(),
                    format!(
                        "container 名 '{name}' が不正です。英小文字始まりの英小文字+数字 \
                         (最大 32 文字、hyphen 不可) にしてください。物理リソース名の \
                         slot とドット記法の一意分解に使うための制約です。"
                    ),
                ));
            }
        }
        Ok(())
    }

    /// container 間の secrets 宣言の整合性 (store / pocket_key_format の一致、
    /// shared managed secret と user secret の同名宣言の spec 一致)
    fn check_container_secrets_consistency(&self) -> Result<(), SettingsError> {
        let err = |m: String| Err(SettingsError::value(Vec::new(), m));
        let mut stores: Vec<(&str, StoreType)> = Vec::new();
        let mut formats: Vec<(&str, &str)> = Vec::new();
        let mut shared: Vec<(&str, &str, &ManagedSecretSpec)> = Vec::new();
        let mut users: Vec<(&str, &str, &UserSecretSpec)> = Vec::new();
        for (name, c) in self.container.iter() {
            let Some(secrets) = &c.secrets else { continue };
            stores.push((name, secrets.store));
            formats.push((name, secrets.pocket_key_format.as_str()));
            for (key, spec) in secrets.managed.iter() {
                if !spec.shared {
                    continue;
                }
                match shared.iter().find(|(k, _, _)| k == key) {
                    Some((_, owner, first)) if *first != spec => {
                        return err(format!(
                            "shared managed secret '{key}' が container '{owner}' と '{name}' で\
                             異なる spec で宣言されています。共有する secret は\
                             全宣言で同一 spec にしてください。"
                        ))
                    }
                    Some(_) => {}
                    None => shared.push((key, name, spec)),
                }
            }
            for (key, spec) in secrets.user.iter() {
                match users.iter().find(|(k, _, _)| k == key) {
                    Some((_, owner, first)) if *first != spec => {
                        return err(format!(
                            "user secret '{key}' が container '{owner}' と '{name}' で異なる \
                             spec で宣言されています。同名 key は同一 spec にするか、\
                             別の key 名を使ってください。"
                        ))
                    }
                    Some(_) => {}
                    None => users.push((key, name, spec)),
                }
            }
        }
        let listed = |pairs: Vec<(&str, String)>| {
            let mut pairs = pairs;
            pairs.sort();
            pairs
                .iter()
                .map(|(n, v)| format!("{n}={v}"))
                .collect::<Vec<_>>()
                .join(", ")
        };
        let distinct_stores: std::collections::BTreeSet<_> =
            stores.iter().map(|(_, s)| *s as u8).collect();
        if distinct_stores.len() > 1 {
            let pairs = stores
                .iter()
                .map(|(n, s)| (*n, store_name(*s).to_string()))
                .collect();
            return err(format!(
                "secrets.store は全 container で一致が必要です: {}",
                listed(pairs)
            ));
        }
        let distinct_formats: std::collections::BTreeSet<_> =
            formats.iter().map(|(_, f)| *f).collect();
        if distinct_formats.len() > 1 {
            let pairs = formats.iter().map(|(n, f)| (*n, f.to_string())).collect();
            return err(format!(
                "secrets.pocket_key_format は全 container で一致が必要です: {}",
                listed(pairs)
            ));
        }
        Ok(())
    }
}

fn store_name(s: StoreType) -> &'static str {
    match s {
        StoreType::Sm => "sm",
        StoreType::Ssm => "ssm",
    }
}
