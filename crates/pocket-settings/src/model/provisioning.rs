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
/// (field 名で書くと [`check_section_keys`] が alias を案内して拒否する)
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
        ],
    ),
    (
        "upstash",
        &[
            ("email", Some("upstash_email")),
            ("api_key", Some("upstash_api_key")),
            ("budget", None),
        ],
    ),
];

/// 廃止したキー (案内つきで拒否する)
const SKIP_CHECK_EXISTING: &str = "skip_check_existing";
const PROVISIONING: &str = "provisioning";
const REMOVED_KEYS: &[&str] = &[PROVISIONING, SKIP_CHECK_EXISTING];

/// toml の key の typo を見る (Python の `check_env_backed_section_keys`)。
/// `extra="ignore"` の Python モデルで typo を拾うための検査
pub(crate) fn check_section_keys(data: &toml::Table) -> Result<(), SettingsError> {
    for (section, fields) in ENV_BACKED_SECTIONS {
        let Some(toml::Value::Table(t)) = data.get(*section) else {
            continue;
        };
        let mut valid: Vec<&str> = fields
            .iter()
            .map(|(name, alias)| alias.unwrap_or(name))
            .collect();
        valid.sort();
        valid.dedup();
        for key in t.keys() {
            if let Some(alias) = fields
                .iter()
                .find(|(name, _)| *name == key.as_str())
                .and_then(|(_, alias)| *alias)
            {
                return Err(SettingsError::value(
                    Vec::new(),
                    format!(
                        "[{section}] {key} は toml では {alias} と書きます \
                         (field 名で書いた値は読まれません)\n  \
                         通常は環境変数 {} または .env で渡してください",
                        alias.to_uppercase()
                    ),
                ));
            }
            if !REMOVED_KEYS.contains(&key.as_str()) && !valid.contains(&key.as_str()) {
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

/// 廃止した `provisioning` / `skip_check_existing` を案内つきで拒否する (Python の
/// `_reject_removed_db_keys`)
pub(crate) fn reject_removed_keys(data: &toml::Table) -> Result<(), SettingsError> {
    for (section, _) in ENV_BACKED_SECTIONS {
        let Some(toml::Value::Table(t)) = data.get(*section) else {
            continue;
        };
        let store_url = format!("`pocket resource {section} store-url --stage <stage>`");
        let message = match t.get(PROVISIONING) {
            Some(toml::Value::String(mode)) if mode == "command" => Some(format!(
                "[{section}] provisioning は廃止されました (deploy は {section} に触れません)。\
                 この行を削除してください。"
            )),
            Some(_) => Some(format!(
                "[{section}] provisioning は廃止されました。deploy は {section} を用意しません。\
                 この行を削除し、接続 URL を [container.<name>.secrets.user] の type で\
                 宣言したうえで、API key がある場所で {store_url} を実行してください。"
            )),
            None if t.contains_key(SKIP_CHECK_EXISTING) => Some(format!(
                "[{section}] skip_check_existing は廃止されました。この行を削除し、接続 URL を \
                 [container.<name>.secrets.user] の type で宣言したうえで deploy 前に \
                 {store_url} を実行してください。"
            )),
            None => None,
        };
        if let Some(message) = message {
            return Err(SettingsError::value(path(&[section]), message));
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
}

fn default_budget() -> i64 {
    20
}

/// Python (pydantic) の lax な int: 整数・小数部が 0 の小数・真偽値 (1 / 0)、
/// 整数を表す文字列 (前後の空白・桁区切りの `_`・小数部が 0 の `16.0` を許す)
fn lax_int<'de, D: serde::Deserializer<'de>>(d: D) -> Result<i64, D::Error> {
    use serde::de::Error as _;
    let integral = |f: f64| (f.fract() == 0.0 && f.is_finite()).then_some(f as i64);
    match toml::Value::deserialize(d)? {
        toml::Value::Integer(i) => Ok(i),
        toml::Value::Boolean(b) => Ok(i64::from(b)),
        toml::Value::Float(f) => integral(f).ok_or_else(|| {
            D::Error::custom("Input should be a valid integer, got a number with a fractional part")
        }),
        toml::Value::String(s) => {
            let s = s.trim().replace('_', "");
            s.parse::<i64>()
                .ok()
                .or_else(|| s.parse::<f64>().ok().and_then(integral))
                .ok_or_else(|| {
                    D::Error::custom(
                        "Input should be a valid integer, unable to parse string as an integer",
                    )
                })
        }
        _ => Err(D::Error::custom("Input should be a valid integer")),
    }
}
