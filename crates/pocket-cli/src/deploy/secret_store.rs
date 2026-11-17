//! pocket managed secret の store (Python 版 `SecretsManager` / `SsmStore`) と、
//! stored user secret の存在確認 (`StoredUserSecretStore`)。
//!
//! - Secrets Manager: 1 つの secret (`pocket_key`) に `{stage: {project: {key: 値}}}` の
//!   JSON を持つ。書き込みは自分の `stage` / `project` の entry だけを置き換える
//! - SSM: `/<pocket_key>/<key>` (値が dict なら `/<pocket_key>/<key>/<sub>`) の SecureString

use std::collections::BTreeSet;

use anyhow::{anyhow, Result};
use aws_config::SdkConfig;
use magic_pocket_settings::context::SecretsContext;
use magic_pocket_settings::model::StoreType;
use magic_pocket_settings::pyjson;
use serde_json::{Map, Value as Json};

use crate::aws_util::{has_code, regional_config, sdk_error};

/// pocket managed secret の中身 (key → 文字列か、sub key → 文字列の dict)
pub type ManagedSecrets = Map<String, Json>;

const NOT_FOUND: &[&str] = &["ParameterNotFound", "ResourceNotFoundException"];

/// DeleteParameters は 1 回 10 件まで
const SSM_DELETE_BATCH: usize = 10;

/// managed secret の読み書き (mediator が使う。テストでは fake に差し替える)
pub trait SecretStore {
    /// store view の managed secret。無ければ空
    async fn read(&self, view: &SecretsContext) -> Result<ManagedSecrets>;

    /// view の managed secret を `secrets` にする。SM は自分の entry の全置換、SSM は
    /// 渡した key の上書き (渡さなかった key は消えない)
    async fn update(&self, view: &SecretsContext, secrets: &ManagedSecrets) -> Result<()>;

    /// 指定した key だけを消す (orphan の掃除用)。「全削除 → 書き戻し」は中断すると
    /// 無関係な secret まで失うので行わない
    async fn delete_keys(&self, view: &SecretsContext, keys: &BTreeSet<String>) -> Result<()>;

    /// stored user secret (正準名 `name`) が store にあるか。値は読まない
    async fn user_secret_exists(&self, name: &str, store: StoreType, region: &str) -> Result<bool>;
}

/// AWS の store。削除予約中の SM secret は復元してから読む (Python 版と同じ)
pub struct AwsSecretStore {
    sdk: SdkConfig,
}

/// Secrets Manager の secret 1 つ分 (全 stage・全 project の値を持つ)
struct SmDocument {
    arn: String,
    data: Map<String, Json>,
}

impl AwsSecretStore {
    pub fn new(sdk: SdkConfig) -> Self {
        Self { sdk }
    }

    fn sm(&self, view: &SecretsContext) -> aws_sdk_secretsmanager::Client {
        aws_sdk_secretsmanager::Client::new(&regional_config(&self.sdk, &view.region))
    }

    fn ssm(&self, view: &SecretsContext) -> aws_sdk_ssm::Client {
        aws_sdk_ssm::Client::new(&regional_config(&self.sdk, &view.region))
    }

    async fn update_sm(&self, view: &SecretsContext, secrets: &ManagedSecrets) -> Result<()> {
        let client = self.sm(view);
        eprintln!("Getting pocket secrets {} ...", view.pocket_key);
        let document = sm_document(&client, view, true).await?;
        let mut data = document
            .as_ref()
            .map(|d| d.data.clone())
            .unwrap_or_default();
        let stage = data
            .entry(view.stage.clone())
            .or_insert_with(|| Json::Object(Map::new()));
        let Some(stage) = stage.as_object_mut() else {
            return Err(broken_document(view));
        };
        stage.insert(view.project_name.clone(), Json::Object(secrets.clone()));
        let body = pyjson::dumps(&Json::Object(data));
        eprintln!("Updating pocket secrets {} ...", view.pocket_key);
        match document {
            None => {
                client
                    .create_secret()
                    .name(&view.pocket_key)
                    .secret_string(body)
                    .send()
                    .await
                    .map_err(|e| sdk_error("secretsmanager create_secret", &e))?;
            }
            Some(document) => {
                client
                    .put_secret_value()
                    .secret_id(document.arn)
                    .secret_string(body)
                    .send()
                    .await
                    .map_err(|e| sdk_error("secretsmanager put_secret_value", &e))?;
            }
        }
        Ok(())
    }

    async fn update_ssm(&self, view: &SecretsContext, secrets: &ManagedSecrets) -> Result<()> {
        let client = self.ssm(view);
        eprintln!("Updating pocket secrets via SSM {} ...", view.pocket_key);
        let mut parameters: Vec<(String, &str)> = Vec::new();
        for (key, value) in secrets {
            match value {
                Json::String(value) => parameters.push((key.clone(), value)),
                Json::Object(subs) => {
                    for (sub, value) in subs {
                        if let Some(value) = value.as_str() {
                            parameters.push((format!("{key}/{sub}"), value));
                        }
                    }
                }
                _ => {}
            }
        }
        for (name, value) in parameters {
            client
                .put_parameter()
                .name(format!("/{}/{name}", view.pocket_key))
                .value(value)
                .r#type(aws_sdk_ssm::types::ParameterType::SecureString)
                .overwrite(true)
                .send()
                .await
                .map_err(|e| sdk_error("ssm put_parameter", &e))?;
        }
        Ok(())
    }

    /// `/<pocket_key>/` 配下の parameter 名のうち `keep` が真のものを消す。戻り値は件数
    async fn delete_ssm_parameters(
        &self,
        view: &SecretsContext,
        keep: impl Fn(&str) -> bool,
    ) -> Result<usize> {
        let client = self.ssm(view);
        let path = format!("/{}/", view.pocket_key);
        let mut pages = client
            .get_parameters_by_path()
            .path(&path)
            .recursive(true)
            .into_paginator()
            .send();
        let mut names = Vec::new();
        while let Some(page) = pages.next().await {
            let page = page.map_err(|e| sdk_error("ssm get_parameters_by_path", &e))?;
            for parameter in page.parameters() {
                let name = parameter.name().unwrap_or_default();
                if keep(name.get(path.len()..).unwrap_or_default()) {
                    names.push(name.to_string());
                }
            }
        }
        for batch in names.chunks(SSM_DELETE_BATCH) {
            client
                .delete_parameters()
                .set_names(Some(batch.to_vec()))
                .send()
                .await
                .map_err(|e| sdk_error("ssm delete_parameters", &e))?;
        }
        Ok(names.len())
    }

    /// view の managed secret を全部消す (Python の `delete_secrets`。destroy 用)。
    ///
    /// SM は自分の `stage` / `project` の entry だけを除き、secret が空になったら
    /// 30 日の復元期間付きで削除する
    pub async fn delete_all(&self, view: &SecretsContext) -> Result<()> {
        match view.store {
            StoreType::Ssm => {
                eprintln!("Deleting pocket secrets via SSM {} ...", view.pocket_key);
                if self.delete_ssm_parameters(view, |_| true).await? == 0 {
                    eprintln!("No SSM pocket secrets found");
                }
                Ok(())
            }
            StoreType::Sm => self.delete_all_sm(view).await,
        }
    }

    async fn delete_all_sm(&self, view: &SecretsContext) -> Result<()> {
        let client = self.sm(view);
        eprintln!("Deleting pocket secrets {} ...", view.pocket_key);
        let Some(mut document) = sm_document(&client, view, true).await? else {
            eprintln!("Pocket secrets key was not found");
            return Ok(());
        };
        let removed = document
            .data
            .get_mut(&view.stage)
            .and_then(Json::as_object_mut)
            .and_then(|stage| stage.shift_remove(&view.project_name))
            .filter(|entry| !entry.is_null());
        if removed.is_none() {
            eprintln!("Pocket secrets entry was not found");
            return Ok(());
        }
        let stage_is_empty = document
            .data
            .get(&view.stage)
            .and_then(Json::as_object)
            .is_some_and(Map::is_empty);
        if stage_is_empty {
            document.data.shift_remove(&view.stage);
        }
        eprintln!("Deleting the entry...");
        let is_empty = document.data.is_empty();
        client
            .put_secret_value()
            .secret_id(&document.arn)
            .secret_string(pyjson::dumps(&Json::Object(document.data)))
            .send()
            .await
            .map_err(|e| sdk_error("secretsmanager put_secret_value", &e))?;
        if is_empty {
            eprintln!("No entry left, deleting the secret {}...", document.arn);
            client
                .delete_secret()
                .secret_id(&document.arn)
                .recovery_window_in_days(30)
                .send()
                .await
                .map_err(|e| sdk_error("secretsmanager delete_secret", &e))?;
        }
        Ok(())
    }
}

impl SecretStore for AwsSecretStore {
    async fn read(&self, view: &SecretsContext) -> Result<ManagedSecrets> {
        Ok(read_managed(&self.sdk, view, true)
            .await?
            .unwrap_or_default())
    }

    async fn update(&self, view: &SecretsContext, secrets: &ManagedSecrets) -> Result<()> {
        match view.store {
            StoreType::Sm => self.update_sm(view, secrets).await,
            StoreType::Ssm => self.update_ssm(view, secrets).await,
        }
    }

    async fn delete_keys(&self, view: &SecretsContext, keys: &BTreeSet<String>) -> Result<()> {
        match view.store {
            StoreType::Sm => {
                let mut cleaned = self.read(view).await?;
                cleaned.retain(|key, _| !keys.contains(key));
                self.update_sm(view, &cleaned).await
            }
            StoreType::Ssm => {
                let listed: Vec<&str> = keys.iter().map(String::as_str).collect();
                eprintln!(
                    "Deleting pocket secrets via SSM {}: {} ...",
                    view.pocket_key,
                    listed.join(", ")
                );
                self.delete_ssm_parameters(view, |relative| {
                    keys.contains(relative.split('/').next().unwrap_or_default())
                })
                .await?;
                Ok(())
            }
        }
    }

    async fn user_secret_exists(&self, name: &str, store: StoreType, region: &str) -> Result<bool> {
        let config = regional_config(&self.sdk, region);
        match store {
            StoreType::Ssm => {
                let client = aws_sdk_ssm::Client::new(&config);
                match client.get_parameter().name(name).send().await {
                    Ok(_) => Ok(true),
                    Err(e) if has_code(&e, NOT_FOUND) => Ok(false),
                    Err(e) => Err(sdk_error("ssm get_parameter", &e)),
                }
            }
            StoreType::Sm => {
                let client = aws_sdk_secretsmanager::Client::new(&config);
                match client.describe_secret().secret_id(name).send().await {
                    Ok(_) => Ok(true),
                    Err(e) if has_code(&e, NOT_FOUND) => Ok(false),
                    Err(e) => Err(sdk_error("secretsmanager describe_secret", &e)),
                }
            }
        }
    }
}

/// store view の managed secret を読む。SM の secret 自体が無ければ `None`。
///
/// `restore` が真なら削除予約中の SM secret を復元してから読み、偽なら無いものとして扱う
/// (`status` は AWS に書かない)
pub async fn read_managed(
    sdk: &SdkConfig,
    view: &SecretsContext,
    restore: bool,
) -> Result<Option<ManagedSecrets>> {
    let config = regional_config(sdk, &view.region);
    match view.store {
        StoreType::Sm => {
            let client = aws_sdk_secretsmanager::Client::new(&config);
            let Some(document) = sm_document(&client, view, restore).await? else {
                return Ok(None);
            };
            let project = document
                .data
                .get(&view.stage)
                .and_then(|stage| stage.get(&view.project_name))
                .and_then(Json::as_object);
            Ok(Some(project.cloned().unwrap_or_default()))
        }
        StoreType::Ssm => {
            let client = aws_sdk_ssm::Client::new(&config);
            Ok(Some(read_ssm(&client, view).await?))
        }
    }
}

async fn sm_document(
    client: &aws_sdk_secretsmanager::Client,
    view: &SecretsContext,
    restore: bool,
) -> Result<Option<SmDocument>> {
    let get = || client.get_secret_value().secret_id(&view.pocket_key).send();
    let response = match get().await {
        Ok(response) => response,
        Err(e) if has_code(&e, &["ResourceNotFoundException"]) => return Ok(None),
        // InvalidRequestException は削除予約中
        Err(e) if has_code(&e, &["InvalidRequestException"]) => {
            if !restore {
                return Ok(None);
            }
            client
                .restore_secret()
                .secret_id(&view.pocket_key)
                .send()
                .await
                .map_err(|e| sdk_error("secretsmanager restore_secret", &e))?;
            get()
                .await
                .map_err(|e| sdk_error("secretsmanager get_secret_value", &e))?
        }
        Err(e) => return Err(sdk_error("secretsmanager get_secret_value", &e)),
    };
    let data: Json = serde_json::from_str(response.secret_string().unwrap_or("{}"))
        .map_err(|_| broken_document(view))?;
    let Json::Object(data) = data else {
        return Err(broken_document(view));
    };
    Ok(Some(SmDocument {
        arn: response.arn().unwrap_or_default().to_string(),
        data,
    }))
}

fn broken_document(view: &SecretsContext) -> anyhow::Error {
    anyhow!(
        "pocket managed secret {} が {{stage: {{project: ...}}}} の JSON ではありません",
        view.pocket_key
    )
}

async fn read_ssm(client: &aws_sdk_ssm::Client, view: &SecretsContext) -> Result<ManagedSecrets> {
    let path = format!("/{}/", view.pocket_key);
    let mut pages = client
        .get_parameters_by_path()
        .path(&path)
        .recursive(true)
        .with_decryption(true)
        .into_paginator()
        .send();
    let mut result = ManagedSecrets::new();
    while let Some(page) = pages.next().await {
        let page = page.map_err(|e| sdk_error("ssm get_parameters_by_path", &e))?;
        for parameter in page.parameters() {
            let name = parameter.name().unwrap_or_default();
            let value = parameter.value().unwrap_or_default();
            // /{pocket_key}/{key} か /{pocket_key}/{key}/{sub}
            let parts: Vec<&str> = name
                .get(path.len()..)
                .unwrap_or_default()
                .split('/')
                .collect();
            match parts.as_slice() {
                [key] => {
                    result.insert(key.to_string(), Json::from(value));
                }
                [key, sub] => {
                    let entry = result
                        .entry(key.to_string())
                        .or_insert_with(|| Json::Object(Map::new()));
                    if let Some(entry) = entry.as_object_mut() {
                        entry.insert(sub.to_string(), Json::from(value));
                    }
                }
                _ => {}
            }
        }
    }
    Ok(result)
}
