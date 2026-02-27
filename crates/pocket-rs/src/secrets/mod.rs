pub mod secretsmanager;
pub mod ssm;

use std::collections::HashMap;

use tracing::info;

use crate::config::{ManagedSecretSpec, PocketConfig, SecretsConfig, StoreType};
use crate::error::{PocketError, Result};

/// シークレットを取得して環境変数名→値の HashMap として返す
///
/// Python の runtime.py:get_secrets() に相当:
/// 1. managed: pocket_store から一括取得 → expand_secret() で展開
/// 2. user: 各 spec の store に応じて SM/SSM から個別取得
pub async fn get_secrets(config: &PocketConfig) -> Result<HashMap<String, String>> {
    let sc = match &config.secrets {
        Some(sc) => sc,
        None => return Ok(HashMap::new()),
    };

    let mut secrets = HashMap::new();

    // managed secrets
    if !sc.managed.is_empty() {
        let raw_secrets = get_managed_secrets(sc).await?;
        for (key, value) in &raw_secrets {
            if let Some(spec) = sc.managed.get(key) {
                let envs = expand_secret(key, value, spec)?;
                secrets.extend(envs);
            }
        }
    }

    // user secrets
    for (key, spec) in &sc.user {
        let effective_store = spec.store.as_ref().unwrap_or(&sc.store);
        let value = match effective_store {
            StoreType::Sm => {
                info!("Fetching user secret from SM: {}", spec.name);
                secretsmanager::get_user_secret(&sc.region, &spec.name).await?
            }
            StoreType::Ssm => {
                info!("Fetching user secret from SSM: {}", spec.name);
                ssm::get_user_secret(&sc.region, &spec.name).await?
            }
        };
        secrets.insert(key.clone(), value);
    }

    Ok(secrets)
}

/// store に応じて SM / SSM から managed secrets を一括取得
async fn get_managed_secrets(
    sc: &SecretsConfig,
) -> Result<HashMap<String, serde_json::Value>> {
    match sc.store {
        StoreType::Sm => {
            info!("Fetching managed secrets from SM: {}", sc.pocket_key);
            secretsmanager::get_pocket_secrets(&sc.region, &sc.pocket_key, &sc.stage, &sc.project_name)
                .await
        }
        StoreType::Ssm => {
            info!("Fetching managed secrets from SSM: {}", sc.pocket_key);
            ssm::get_pocket_secrets(&sc.region, &sc.pocket_key).await
        }
    }
}

/// シークレット値を環境変数に展開する
///
/// Python の runtime.py:_pocket_secret_to_envs() に相当:
/// - string 値 → {key: value}
/// - rsa_pem_base64 / cloudfront_signing_key → {key+pem_suffix: pem, key+pub_suffix: pub}
fn expand_secret(
    key: &str,
    value: &serde_json::Value,
    spec: &ManagedSecretSpec,
) -> Result<HashMap<String, String>> {
    let mut result = HashMap::new();

    match value {
        serde_json::Value::String(s) => {
            result.insert(key.to_string(), s.clone());
        }
        serde_json::Value::Object(obj) => match spec.secret_type.as_str() {
            "rsa_pem_base64" | "cloudfront_signing_key" => {
                let pem_suffix = spec
                    .options
                    .get("pem_base64_environ_suffix")
                    .map(|s| s.as_str())
                    .unwrap_or("_PEM_BASE64");
                let pub_suffix = spec
                    .options
                    .get("pub_base64_environ_suffix")
                    .map(|s| s.as_str())
                    .unwrap_or("_PUB_BASE64");

                if let Some(pem) = obj.get("pem").and_then(|v| v.as_str()) {
                    result.insert(format!("{}{}", key, pem_suffix), pem.to_string());
                }
                if let Some(pub_val) = obj.get("pub").and_then(|v| v.as_str()) {
                    result.insert(format!("{}{}", key, pub_suffix), pub_val.to_string());
                }
            }
            other => {
                return Err(PocketError::UnsupportedSecretType(format!(
                    "key={}, type={}",
                    key, other
                )));
            }
        },
        _ => {
            return Err(PocketError::UnsupportedSecretType(format!(
                "unexpected value type for key={}",
                key
            )));
        }
    }

    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_spec(secret_type: &str, options: Vec<(&str, &str)>) -> ManagedSecretSpec {
        ManagedSecretSpec {
            secret_type: secret_type.to_string(),
            options: options
                .into_iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect(),
        }
    }

    #[test]
    fn test_expand_secret_string() {
        let spec = make_spec("password", vec![]);
        let value = serde_json::Value::String("my_password".to_string());
        let result = expand_secret("SECRET_KEY", &value, &spec).unwrap();
        assert_eq!(result.len(), 1);
        assert_eq!(result["SECRET_KEY"], "my_password");
    }

    #[test]
    fn test_expand_secret_rsa_pem_default_suffixes() {
        let spec = make_spec("rsa_pem_base64", vec![]);
        let value = serde_json::json!({
            "pem": "base64_pem_data",
            "pub": "base64_pub_data"
        });
        let result = expand_secret("MY_KEY", &value, &spec).unwrap();
        assert_eq!(result.len(), 2);
        assert_eq!(result["MY_KEY_PEM_BASE64"], "base64_pem_data");
        assert_eq!(result["MY_KEY_PUB_BASE64"], "base64_pub_data");
    }

    #[test]
    fn test_expand_secret_rsa_pem_custom_suffixes() {
        let spec = make_spec(
            "rsa_pem_base64",
            vec![
                ("pem_base64_environ_suffix", "_PRIVATE"),
                ("pub_base64_environ_suffix", "_PUBLIC"),
            ],
        );
        let value = serde_json::json!({
            "pem": "pem_data",
            "pub": "pub_data"
        });
        let result = expand_secret("CF_KEY", &value, &spec).unwrap();
        assert_eq!(result.len(), 2);
        assert_eq!(result["CF_KEY_PRIVATE"], "pem_data");
        assert_eq!(result["CF_KEY_PUBLIC"], "pub_data");
    }

    #[test]
    fn test_expand_secret_cloudfront_signing_key() {
        let spec = make_spec("cloudfront_signing_key", vec![]);
        let value = serde_json::json!({
            "pem": "cf_pem",
            "pub": "cf_pub"
        });
        let result = expand_secret("SIGNING", &value, &spec).unwrap();
        assert_eq!(result.len(), 2);
        assert_eq!(result["SIGNING_PEM_BASE64"], "cf_pem");
        assert_eq!(result["SIGNING_PUB_BASE64"], "cf_pub");
    }

    #[test]
    fn test_expand_secret_unsupported_type() {
        let spec = make_spec("unknown_type", vec![]);
        let value = serde_json::json!({"key": "val"});
        let err = expand_secret("X", &value, &spec).unwrap_err();
        assert!(matches!(err, PocketError::UnsupportedSecretType(_)));
    }
}
