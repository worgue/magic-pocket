//! `[neon]` / `[tidb]` / `[upstash]` (Python の `Neon` / `TiDb` / `Upstash`)。
//!
//! Python では `BaseSettings` で、toml に無い field を環境変数と `.env` から読む。
//! 環境変数名は alias があれば alias、無ければ field 名 (大文字小文字を区別しない)。
//! 優先順位は toml > 環境変数 > `.env` > 既定値。Rust では前処理
//! ([`fill_from_env`]) で toml の table に環境の値を足してから読む。
//! strict の対象外なので、整数の field は文字列 (環境変数の値) も受ける。

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::error::{path, SettingsError};
use crate::types::FormatStr;

/// section ごとの (field 名, alias)。alias のある field は toml でも alias で書く
/// (Python は field 名で書いた値を黙って捨てる。互換のため同じにする)
/// (field 名, alias)
type EnvField = (&'static str, Option<&'static str>);

pub(crate) const ENV_BACKED_SECTIONS: &[(&str, &[EnvField])] = &[
    (
        "neon",
        &[
            ("project_name", None),
            ("pg_version", None),
            ("branch_name", None),
            ("parent_branch_name", None),
            ("api_key", Some("neon_api_key")),
            ("provisioning", None),
        ],
    ),
    (
        "tidb",
        &[
            ("public_key", Some("tidb_public_key")),
            ("private_key", Some("tidb_private_key")),
            ("project", None),
            ("cluster", None),
            ("region", None),
            ("provisioning", None),
        ],
    ),
    (
        "upstash",
        &[
            ("email", Some("upstash_email")),
            ("api_key", Some("upstash_api_key")),
            ("budget", None),
            ("provisioning", None),
        ],
    ),
];

/// 廃止したキー (案内つきで拒否する)
const SKIP_CHECK_EXISTING: &str = "skip_check_existing";

/// toml の key の typo を見る (Python の `check_env_backed_section_keys`)。
/// `extra="ignore"` の Python モデルで typo を拾うための検査
pub(crate) fn check_section_keys(data: &toml::Table) -> Result<(), SettingsError> {
    for (section, fields) in ENV_BACKED_SECTIONS {
        let Some(toml::Value::Table(t)) = data.get(*section) else {
            continue;
        };
        let mut valid: Vec<&str> = fields
            .iter()
            .flat_map(|(name, alias)| std::iter::once(*name).chain(*alias))
            .collect();
        valid.sort();
        valid.dedup();
        for key in t.keys() {
            if key != SKIP_CHECK_EXISTING && !valid.contains(&key.as_str()) {
                let listed: Vec<String> = valid.iter().map(|k| format!("'{k}'")).collect();
                return Err(SettingsError::value(
                    Vec::new(),
                    format!(
                        "invalid key {key} in [{section}] of pocket.toml\n  有効なキー: [{}]",
                        listed.join(", ")
                    ),
                ));
            }
        }
    }
    Ok(())
}

/// 廃止した `skip_check_existing` を案内つきで拒否する (Python の
/// `_reject_skip_check_existing`)
pub(crate) fn reject_skip_check_existing(data: &toml::Table) -> Result<(), SettingsError> {
    for (section, _) in ENV_BACKED_SECTIONS {
        let Some(toml::Value::Table(t)) = data.get(*section) else {
            continue;
        };
        if t.contains_key(SKIP_CHECK_EXISTING) {
            return Err(SettingsError::value(
                path(&[section]),
                format!(
                    "[{section}] skip_check_existing は廃止されました。\
                     `provisioning = \"command\"` に置き換え、接続 URL を \
                     [container.<name>.secrets.user] の type で宣言したうえで deploy 前に \
                     `pocket resource {section} store-url --stage <stage>` を実行してください。"
                ),
            ));
        }
    }
    Ok(())
}

/// toml に無い field を環境から補い、alias の key を field 名に直す
pub(crate) fn fill_from_env(data: &mut toml::Table, env: &BTreeMap<String, String>) {
    let lookup = |name: &str| {
        env.iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.clone())
    };
    for (section, fields) in ENV_BACKED_SECTIONS {
        let Some(toml::Value::Table(t)) = data.get_mut(*section) else {
            continue;
        };
        let mut filled = toml::Table::new();
        for (name, alias) in *fields {
            let key = alias.unwrap_or(name);
            let value = t
                .get(key)
                .cloned()
                .or_else(|| lookup(key).map(toml::Value::String));
            if let Some(v) = value {
                filled.insert((*name).to_string(), v);
            }
        }
        *t = filled;
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ProvisioningMode {
    #[default]
    Deploy,
    Command,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Neon {
    pub project_name: String,
    #[serde(default = "default_pg_version", deserialize_with = "lax_int")]
    pub pg_version: i64,
    #[serde(default)]
    pub branch_name: Option<FormatStr>,
    #[serde(default)]
    pub parent_branch_name: Option<FormatStr>,
    #[serde(default)]
    pub api_key: Option<String>,
    #[serde(default)]
    pub provisioning: ProvisioningMode,
}

fn default_pg_version() -> i64 {
    15
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct TiDb {
    #[serde(default)]
    pub public_key: Option<String>,
    #[serde(default)]
    pub private_key: Option<String>,
    #[serde(default)]
    pub project: Option<String>,
    #[serde(default)]
    pub cluster: Option<String>,
    #[serde(default = "default_tidb_region")]
    pub region: String,
    #[serde(default)]
    pub provisioning: ProvisioningMode,
}

fn default_tidb_region() -> String {
    "ap-northeast-1".to_string()
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Upstash {
    #[serde(default)]
    pub email: Option<String>,
    #[serde(default)]
    pub api_key: Option<String>,
    #[serde(default = "default_budget", deserialize_with = "lax_int")]
    pub budget: i64,
    #[serde(default)]
    pub provisioning: ProvisioningMode,
}

fn default_budget() -> i64 {
    20
}

/// 整数か整数の文字列 (環境変数の値) を受ける (Python の lax な int)
fn lax_int<'de, D: serde::Deserializer<'de>>(d: D) -> Result<i64, D::Error> {
    use serde::de::Error as _;
    match toml::Value::deserialize(d)? {
        toml::Value::Integer(i) => Ok(i),
        toml::Value::Float(f) if f.fract() == 0.0 && f.is_finite() => Ok(f as i64),
        toml::Value::String(s) => s.trim().parse().map_err(|_| {
            D::Error::custom(
                "Input should be a valid integer, unable to parse string as an integer",
            )
        }),
        _ => Err(D::Error::custom("Input should be a valid integer")),
    }
}
