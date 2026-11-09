//! pocket.toml の設定モデル。
//!
//! Python の `pocket.settings` (`Settings` / `GeneralSettings` / `pocket.django.settings`)
//! と同じ形・同じ検証で読む。一致は共有 test vector (`tests/data/settings_vectors`)
//! で固定している。読み込みは 2 層:
//!
//! - [`Settings::load`]: ファイルと環境 (cwd・環境変数) から読む
//! - [`Settings::from_parts`]: toml の本文と [`LoadContext`] だけから読む (vector はこちら)

mod error;
mod load;
pub mod model;
mod types;

pub use error::{ErrorKind, PathSeg, SettingsError};
pub use load::{Advisory, LoadContext};
pub use model::Settings;
pub use types::{AnyValue, TagStr};

/// `pocket settings` が表示時に伏せる key (Python の `SECRET_DUMP_KEYS`)
pub const SECRET_DUMP_KEYS: &[&str] = &[
    "api_key",
    "private_key",
    "public_key",
    "token_secret",
    "password",
];

/// 表示用 JSON の secret の値を `****` にする。null は「未設定」として残す
/// (Python の `mask_secret_values`)
pub fn mask_secret_values(value: &serde_json::Value) -> serde_json::Value {
    match value {
        serde_json::Value::Object(obj) => serde_json::Value::Object(
            obj.iter()
                .map(|(k, v)| {
                    let masked = if SECRET_DUMP_KEYS.contains(&k.as_str()) && !v.is_null() {
                        serde_json::Value::String("****".to_string())
                    } else {
                        mask_secret_values(v)
                    };
                    (k.clone(), masked)
                })
                .collect(),
        ),
        serde_json::Value::Array(a) => {
            serde_json::Value::Array(a.iter().map(mask_secret_values).collect())
        }
        other => other.clone(),
    }
}
