use std::collections::HashMap;

use aws_sdk_secretsmanager::Client;
use tracing::warn;

use crate::error::{PocketError, Result};

/// SM から pocket managed secrets を取得する
///
/// Python の SecretsManager.secrets プロパティに相当:
/// - get_secret_value(SecretId=pocket_key) で JSON 取得
/// - ResourceNotFound → 空 HashMap
/// - InvalidRequestException → restore_secret() → リトライ
/// - JSON 構造: { stage: { project_name: { KEY: "value", ... } } }
pub async fn get_pocket_secrets(
    region: &str,
    pocket_key: &str,
    stage: &str,
    project_name: &str,
) -> Result<HashMap<String, serde_json::Value>> {
    let sdk_config = aws_config::defaults(aws_config::BehaviorVersion::latest())
        .region(aws_config::Region::new(region.to_string()))
        .load()
        .await;
    let client = Client::new(&sdk_config);

    let secret_string = match get_secret_string(&client, pocket_key).await {
        Ok(s) => s,
        Err(PocketError::SecretsManager(ref msg)) if msg.contains("ResourceNotFoundException") => {
            return Ok(HashMap::new());
        }
        Err(PocketError::SecretsManager(ref msg)) if msg.contains("InvalidRequestException") => {
            // 削除済みシークレットを復元してリトライ
            warn!("Secret was deleted, restoring: {}", pocket_key);
            restore_secret(&client, pocket_key).await?;
            get_secret_string(&client, pocket_key).await?
        }
        Err(e) => return Err(e),
    };

    let data: serde_json::Value = serde_json::from_str(&secret_string)?;

    // data[stage][project_name] を抽出
    let secrets = data
        .get(stage)
        .and_then(|s| s.get(project_name))
        .and_then(|p| p.as_object())
        .map(|obj| {
            obj.iter()
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect::<HashMap<String, serde_json::Value>>()
        })
        .unwrap_or_default();

    Ok(secrets)
}

/// SM からユーザー指定のシークレットを1件取得
pub async fn get_user_secret(region: &str, secret_id: &str) -> Result<String> {
    let sdk_config = aws_config::defaults(aws_config::BehaviorVersion::latest())
        .region(aws_config::Region::new(region.to_string()))
        .load()
        .await;
    let client = Client::new(&sdk_config);
    get_secret_string(&client, secret_id).await
}

async fn get_secret_string(client: &Client, secret_id: &str) -> Result<String> {
    let resp = client
        .get_secret_value()
        .secret_id(secret_id)
        .send()
        .await
        .map_err(|e| PocketError::SecretsManager(e.to_string()))?;

    resp.secret_string()
        .map(|s| s.to_string())
        .ok_or_else(|| PocketError::SecretsManager("SecretString is empty".into()))
}

async fn restore_secret(client: &Client, secret_id: &str) -> Result<()> {
    client
        .restore_secret()
        .secret_id(secret_id)
        .send()
        .await
        .map_err(|e| PocketError::SecretsManager(e.to_string()))?;
    Ok(())
}
