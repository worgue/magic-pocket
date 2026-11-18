//! `[neon]` / `[tidb]` (Python の `Neon` / `TiDb`) と、廃止したキーの案内。
//!
//! deploy は provider の API を呼ばない。API key は設定に持たず、
//! `pocket resource neon` のコマンドだけが環境変数 `NEON_API_KEY` を読む。

use serde::{Deserialize, Serialize};

use crate::error::{path, PathSeg, SettingsError};
use crate::types::FormatStr;

const NEON_STORE_URL: &str = "`pocket resource neon store-url --stage <stage>`";
const SELF_MANAGED_URL: &str = "接続 URL は自分で Secrets Manager / SSM に保存し、\
     [container.<name>.secrets.user] に `{ name = \"<secret 名>\" }` で宣言してください。";

/// `[tidb]` の廃止したキー (Python の `_REMOVED_TIDB_KEYS` と同じ順)
const REMOVED_TIDB_KEYS: &[&str] = &[
    "provisioning",
    "skip_check_existing",
    "project",
    "cluster",
    "region",
    "tidb_public_key",
    "tidb_private_key",
    "public_key",
    "private_key",
];

/// 廃止した user secret の type と、案内に出す provider 名
const REMOVED_USER_SECRET_TYPES: &[(&str, &str)] = &[
    ("tidb_database_url", "TiDB"),
    ("upstash_redis_url", "Upstash"),
];

/// 廃止した節・キー・type を案内つきで拒否する (Python の `mode="before"` validator)
pub(crate) fn reject_removed_keys(data: &toml::Table) -> Result<(), SettingsError> {
    if data.contains_key("upstash") {
        return Err(SettingsError::value(
            Vec::new(),
            format!(
                "[upstash] は廃止されました (pocket は Upstash の API を呼びません)。\
                 この節を削除してください。database は Upstash のコンソールで\
                 用意します。{SELF_MANAGED_URL}"
            ),
        ));
    }
    reject_removed_user_secret_types(data)?;
    if let Some(toml::Value::Table(t)) = data.get("neon") {
        if let Some(message) = removed_neon_key(t) {
            return Err(SettingsError::value(path(&["neon"]), message));
        }
    }
    if let Some(toml::Value::Table(t)) = data.get("tidb") {
        if let Some(key) = REMOVED_TIDB_KEYS.iter().find(|k| t.contains_key(**k)) {
            return Err(SettingsError::value(
                path(&["tidb"]),
                format!(
                    "[tidb] {key} は廃止されました (pocket は TiDB Cloud の API を\
                     呼びません)。この行を削除してください。cluster は TiDB Cloud の\
                     コンソールで用意します。{SELF_MANAGED_URL}"
                ),
            ));
        }
    }
    Ok(())
}

fn removed_neon_key(t: &toml::Table) -> Option<String> {
    match t.get("provisioning") {
        Some(toml::Value::String(mode)) if mode == "command" => {
            return Some(
                "[neon] provisioning は廃止されました (deploy は neon に触れません)。\
                 この行を削除してください。"
                    .to_string(),
            )
        }
        Some(_) => {
            return Some(format!(
                "[neon] provisioning は廃止されました。deploy は neon を用意しません。\
                 この行を削除し、接続 URL を [container.<name>.secrets.user] の type で\
                 宣言したうえで、API key がある場所で {NEON_STORE_URL} を実行してください。"
            ))
        }
        None => {}
    }
    if t.contains_key("skip_check_existing") {
        return Some(format!(
            "[neon] skip_check_existing は廃止されました。この行を削除し、接続 URL を \
             [container.<name>.secrets.user] の type で宣言したうえで deploy 前に \
             {NEON_STORE_URL} を実行してください。"
        ));
    }
    ["neon_api_key", "api_key"]
        .iter()
        .find(|k| t.contains_key(**k))
        .map(|key| {
            format!(
                "[neon] {key} は廃止されました。API key は pocket.toml に書かず、\
                 `pocket resource neon` を実行する環境変数 NEON_API_KEY で\
                 渡してください。"
            )
        })
}

/// `[container.<name>.secrets.user]` の廃止した type
fn reject_removed_user_secret_types(data: &toml::Table) -> Result<(), SettingsError> {
    let Some(toml::Value::Table(containers)) = data.get("container") else {
        return Ok(());
    };
    for (name, container) in containers {
        let user = container
            .get("secrets")
            .and_then(|s| s.get("user"))
            .and_then(toml::Value::as_table);
        for (key, spec) in user.into_iter().flatten() {
            let Some(secret_type) = spec.get("type").and_then(toml::Value::as_str) else {
                continue;
            };
            let Some((_, label)) = REMOVED_USER_SECRET_TYPES
                .iter()
                .find(|(t, _)| *t == secret_type)
            else {
                continue;
            };
            let at = ["container", name.as_str(), "secrets", "user", key.as_str()]
                .iter()
                .map(|s| PathSeg::Key((*s).to_string()))
                .collect();
            return Err(SettingsError::value(
                at,
                format!(
                    "type = \"{secret_type}\" は廃止されました (pocket は {label} の API を\
                     呼びません)。{SELF_MANAGED_URL}"
                ),
            ));
        }
    }
    Ok(())
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Neon {
    pub project_name: String,
    #[serde(default = "default_pg_version")]
    pub pg_version: i64,
    #[serde(default)]
    pub branch_name: Option<FormatStr>,
    #[serde(default)]
    pub parent_branch_name: Option<FormatStr>,
}

fn default_pg_version() -> i64 {
    15
}

/// Django の DB engine を django_tidb にする目印 (キーを持たない)
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct TiDb {}
