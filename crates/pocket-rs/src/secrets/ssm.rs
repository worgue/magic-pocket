use std::collections::HashMap;

use aws_sdk_ssm::Client;

use crate::error::{PocketError, Result};

/// SSM Parameter Store から pocket managed secrets を取得する
///
/// Python の SsmStore._pocket_secrets_cache に相当:
/// - get_parameters_by_path(Path=/{pocket_key}/, Recursive=true, WithDecryption=true)
/// - 1階層パラメータ → String 値
/// - 2階層パラメータ → Dict 値 (serde_json::Value::Object)
pub async fn get_pocket_secrets(
    region: &str,
    pocket_key: &str,
) -> Result<HashMap<String, serde_json::Value>> {
    let sdk_config = aws_config::defaults(aws_config::BehaviorVersion::latest())
        .region(aws_config::Region::new(region.to_string()))
        .load()
        .await;
    let client = Client::new(&sdk_config);

    let path = format!("/{}/", pocket_key);
    let mut result: HashMap<String, serde_json::Value> = HashMap::new();

    let mut next_token: Option<String> = None;
    loop {
        let mut req = client
            .get_parameters_by_path()
            .path(&path)
            .recursive(true)
            .with_decryption(true);

        if let Some(token) = &next_token {
            req = req.next_token(token);
        }

        let resp = req
            .send()
            .await
            .map_err(|e| PocketError::Ssm(e.to_string()))?;

        for param in resp.parameters() {
            let name = param.name().unwrap_or_default();
            let value = param.value().unwrap_or_default();

            // /{pocket_key}/ 以降の相対パスを取得
            let relative = &name[path.len()..];
            let parts: Vec<&str> = relative.split('/').collect();

            match parts.len() {
                1 => {
                    result.insert(
                        parts[0].to_string(),
                        serde_json::Value::String(value.to_string()),
                    );
                }
                2 => {
                    let env_key = parts[0].to_string();
                    let sub_key = parts[1].to_string();
                    let entry = result
                        .entry(env_key)
                        .or_insert_with(|| serde_json::Value::Object(serde_json::Map::new()));
                    if let Some(obj) = entry.as_object_mut() {
                        obj.insert(sub_key, serde_json::Value::String(value.to_string()));
                    }
                }
                _ => {} // 3階層以上は無視
            }
        }

        next_token = resp.next_token().map(|s| s.to_string());
        if next_token.is_none() {
            break;
        }
    }

    Ok(result)
}

/// SSM からユーザー指定のパラメータを1件取得
pub async fn get_user_secret(region: &str, name: &str) -> Result<String> {
    let sdk_config = aws_config::defaults(aws_config::BehaviorVersion::latest())
        .region(aws_config::Region::new(region.to_string()))
        .load()
        .await;
    let client = Client::new(&sdk_config);

    let resp = client
        .get_parameter()
        .name(name)
        .with_decryption(true)
        .send()
        .await
        .map_err(|e| PocketError::Ssm(e.to_string()))?;

    resp.parameter()
        .and_then(|p| p.value())
        .map(|s| s.to_string())
        .ok_or_else(|| PocketError::Ssm("Parameter value is empty".into()))
}
