//! template と KVS に入る値を managed secret から取り出す (Python 版の各 resource の
//! `_prepare_*`)。
//!
//! store を読んだ後の取り出しだけを持つ (読むのは呼び手)。status と deploy が同じ
//! 取り出しと警告を使う。

use std::collections::BTreeMap;

use anyhow::{anyhow, Result};
use base64::Engine as _;
use magic_pocket_settings::context::{Context, SecretsContext};
use serde_json::Value as Json;

use super::secret_store::ManagedSecrets;
use crate::status::sink::Sink;

/// origin verify の secret を入れる managed secret の key (Python の `ORIGIN_VERIFY_SECRET_KEY`)
pub const ORIGIN_VERIFY_SECRET_KEY: &str = "POCKET_ORIGIN_VERIFY_SECRET";

/// managed secret の key を持つ store view (shared → 名前順の container)。
/// Python の `Context.managed_secret_view`
pub fn managed_secret_view<'c>(ctx: &'c Context, key: &str) -> Option<&'c SecretsContext> {
    if let Some(shared) = ctx.secrets.as_ref() {
        if shared.managed.contains_key(key) {
            return Some(shared);
        }
    }
    let mut names: Vec<&String> = ctx.container.keys().collect();
    names.sort();
    names.into_iter().find_map(|name| {
        ctx.container
            .get(name)
            .and_then(|c| c.secrets.as_ref())
            .filter(|view| view.managed.contains_key(key))
    })
}

/// 署名用の公開鍵 (PEM)。無ければ空文字
pub fn signing_public_key_pem(
    key: &str,
    secrets: &ManagedSecrets,
    sink: &mut dyn Sink,
) -> Result<String> {
    let Some(data) = secrets.get(key) else {
        sink.warning(&format!(
            "signing_key '{key}' not found in managed secrets. \
             Deploy the container first to generate the key."
        ));
        return Ok(String::new());
    };
    let Some(public) = data.get("pub").and_then(Json::as_str) else {
        return Ok(String::new());
    };
    let decoded = base64::engine::general_purpose::STANDARD
        .decode(public)
        .map_err(|e| anyhow!("signing_key '{key}' の公開鍵を base64 として読めません: {e}"))?;
    Ok(String::from_utf8(decoded)?)
}

/// WAF の allow rule が照合する header secret (文字列のものだけ)
pub fn waf_allow_secret_values(
    keys: &[String],
    secrets: &ManagedSecrets,
) -> BTreeMap<String, String> {
    keys.iter()
        .filter_map(|key| {
            let value = secrets.get(key).and_then(Json::as_str)?;
            Some((key.clone(), value.to_string()))
        })
        .collect()
}

/// origin verify の secret。無ければ警告して空文字
pub fn origin_verify_secret(secrets: &ManagedSecrets, sink: &mut dyn Sink) -> String {
    match secrets.get(ORIGIN_VERIFY_SECRET_KEY).and_then(Json::as_str) {
        Some(value) => value.to_string(),
        None => {
            sink.warning(&format!(
                "origin verify secret '{ORIGIN_VERIFY_SECRET_KEY}' が managed secrets に見つかりません"
            ));
            String::new()
        }
    }
}

/// KVS に書く token_secret の値。無い・文字列でないときは警告して `None`
pub fn token_secret_value(
    key: &str,
    secrets: &ManagedSecrets,
    sink: &mut dyn Sink,
) -> Option<String> {
    match secrets.get(key) {
        Some(Json::String(value)) => Some(value.clone()),
        Some(_) => {
            sink.warning(&format!("token_secret の値が文字列ではありません: {key}"));
            None
        }
        None => {
            sink.warning(&format!(
                "token_secret '{key}' が managed secrets に見つかりません"
            ));
            None
        }
    }
}

/// KVS に書く basic 認証の期待値 (`Basic <base64(user:pass)>`)。
/// 値が `user:pass` の形でなければ警告して `None`
pub fn basic_auth_expected(
    key: &str,
    secrets: &ManagedSecrets,
    sink: &mut dyn Sink,
) -> Option<String> {
    match secrets.get(key).and_then(Json::as_str) {
        Some(value) if value.contains(':') => Some(format!(
            "Basic {}",
            base64::engine::general_purpose::STANDARD.encode(value)
        )),
        _ => {
            sink.warning(&format!(
                "basic_auth '{key}' の値が 'user:pass' 形式ではありません"
            ));
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;
    use crate::status::sink::Recorded;

    fn secrets(value: Json) -> ManagedSecrets {
        value.as_object().expect("object").clone()
    }

    #[test]
    fn test_basic_auth_expected_encodes_user_and_password() {
        let mut sink = Recorded::default();
        let stored = secrets(json!({"AUTH": "user:pass", "BAD": "nocolon"}));
        assert_eq!(
            basic_auth_expected("AUTH", &stored, &mut sink).as_deref(),
            Some("Basic dXNlcjpwYXNz")
        );
        assert!(sink.0.is_empty());
        assert_eq!(basic_auth_expected("BAD", &stored, &mut sink), None);
        assert_eq!(basic_auth_expected("NONE", &stored, &mut sink), None);
        assert_eq!(sink.0.len(), 2);
    }

    #[test]
    fn test_token_secret_value_requires_string() {
        let mut sink = Recorded::default();
        let stored = secrets(json!({"TOKEN": "abc", "PAIR": {"pem": "x"}}));
        assert_eq!(
            token_secret_value("TOKEN", &stored, &mut sink).as_deref(),
            Some("abc")
        );
        assert_eq!(token_secret_value("PAIR", &stored, &mut sink), None);
        assert_eq!(token_secret_value("NONE", &stored, &mut sink), None);
        assert_eq!(sink.0.len(), 2);
    }

    #[test]
    fn test_signing_public_key_pem_decodes_pub() {
        let mut sink = Recorded::default();
        let pem = "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n";
        let encoded = base64::engine::general_purpose::STANDARD.encode(pem);
        let stored = secrets(json!({"KEY": {"pem": "x", "pub": encoded}}));
        assert_eq!(
            signing_public_key_pem("KEY", &stored, &mut sink).expect("pem"),
            pem
        );
        assert_eq!(
            signing_public_key_pem("NONE", &stored, &mut sink).expect("空"),
            ""
        );
        assert_eq!(sink.0.len(), 1);
    }

    #[test]
    fn test_waf_allow_secret_values_keeps_strings_only() {
        let stored = secrets(json!({"A": "1", "B": {"x": "y"}}));
        let keys = ["A".to_string(), "B".to_string(), "C".to_string()];
        let values = waf_allow_secret_values(&keys, &stored);
        assert_eq!(values.len(), 1);
        assert_eq!(values["A"], "1");
    }
}
