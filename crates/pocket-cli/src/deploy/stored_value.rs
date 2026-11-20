//! 正準名の値 1 つの読み書き (Python 版 `pocket/secret_store.py` の
//! `put_stored_value` / `read_stored_value` / `delete_stored_value`)。
//!
//! RDS の static な認証情報と、DSQL の endpoint の公開が使う。

use anyhow::Result;
use aws_config::SdkConfig;
use magic_pocket_settings::model::StoreType;

use crate::aws_util::{has_code, regional_config, sdk_error};

const NOT_FOUND: &[&str] = &["ParameterNotFound", "ResourceNotFoundException"];

/// 書き込みが作成だったか更新だったか (表示の出し分けに使う)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PutResult {
    Created,
    Updated,
}

pub trait StoredValues {
    /// SSM は SecureString で上書き、SM は作成 (既にあれば値を更新)
    async fn put(
        &self,
        name: &str,
        store: StoreType,
        value: &str,
        region: &str,
    ) -> Result<PutResult>;
    /// 値。無ければ `None`
    async fn read(&self, name: &str, store: StoreType, region: &str) -> Result<Option<String>>;
    /// 消す。無ければ何もしない (SM は復旧期間なしで即時に消す)
    async fn delete(&self, name: &str, store: StoreType, region: &str) -> Result<()>;
}

pub struct AwsStoredValues<'a> {
    pub sdk: &'a SdkConfig,
}

impl AwsStoredValues<'_> {
    fn ssm(&self, region: &str) -> aws_sdk_ssm::Client {
        aws_sdk_ssm::Client::new(&regional_config(self.sdk, region))
    }

    fn sm(&self, region: &str) -> aws_sdk_secretsmanager::Client {
        aws_sdk_secretsmanager::Client::new(&regional_config(self.sdk, region))
    }
}

impl StoredValues for AwsStoredValues<'_> {
    async fn put(
        &self,
        name: &str,
        store: StoreType,
        value: &str,
        region: &str,
    ) -> Result<PutResult> {
        if store == StoreType::Ssm {
            self.ssm(region)
                .put_parameter()
                .name(name)
                .value(value)
                .r#type(aws_sdk_ssm::types::ParameterType::SecureString)
                .overwrite(true)
                .send()
                .await
                .map_err(|e| sdk_error("ssm put_parameter", &e))?;
            return Ok(PutResult::Created);
        }
        let client = self.sm(region);
        let tag = aws_sdk_secretsmanager::types::Tag::builder()
            .key("Name")
            .value(name)
            .build();
        let created = client
            .create_secret()
            .name(name)
            .secret_string(value)
            .tags(tag)
            .send()
            .await;
        match created {
            Ok(_) => Ok(PutResult::Created),
            Err(e) if has_code(&e, &["ResourceExistsException"]) => {
                client
                    .put_secret_value()
                    .secret_id(name)
                    .secret_string(value)
                    .send()
                    .await
                    .map_err(|e| sdk_error("secretsmanager put_secret_value", &e))?;
                Ok(PutResult::Updated)
            }
            Err(e) => Err(sdk_error("secretsmanager create_secret", &e)),
        }
    }

    async fn read(&self, name: &str, store: StoreType, region: &str) -> Result<Option<String>> {
        if store == StoreType::Ssm {
            let response = self
                .ssm(region)
                .get_parameter()
                .name(name)
                .with_decryption(true)
                .send()
                .await;
            return match response {
                Ok(response) => Ok(response
                    .parameter()
                    .and_then(|parameter| parameter.value())
                    .map(str::to_string)),
                Err(e) if has_code(&e, NOT_FOUND) => Ok(None),
                Err(e) => Err(sdk_error("ssm get_parameter", &e)),
            };
        }
        match self
            .sm(region)
            .get_secret_value()
            .secret_id(name)
            .send()
            .await
        {
            Ok(response) => Ok(response.secret_string().map(str::to_string)),
            Err(e) if has_code(&e, NOT_FOUND) => Ok(None),
            Err(e) => Err(sdk_error("secretsmanager get_secret_value", &e)),
        }
    }

    async fn delete(&self, name: &str, store: StoreType, region: &str) -> Result<()> {
        if store == StoreType::Ssm {
            return match self.ssm(region).delete_parameter().name(name).send().await {
                Ok(_) => Ok(()),
                Err(e) if has_code(&e, NOT_FOUND) => Ok(()),
                Err(e) => Err(sdk_error("ssm delete_parameter", &e)),
            };
        }
        let deleted = self
            .sm(region)
            .delete_secret()
            .secret_id(name)
            .force_delete_without_recovery(true)
            .send()
            .await;
        match deleted {
            Ok(_) => Ok(()),
            Err(e) if has_code(&e, NOT_FOUND) => Ok(()),
            Err(e) => Err(sdk_error("secretsmanager delete_secret", &e)),
        }
    }
}

#[cfg(test)]
pub mod fake {
    use std::cell::RefCell;
    use std::collections::BTreeMap;

    use super::*;

    /// (store, 名前) → 値。呼び出しを記録する
    #[derive(Default)]
    pub struct FakeStoredValues {
        pub values: RefCell<BTreeMap<(String, String), String>>,
        pub calls: RefCell<Vec<String>>,
        /// `true` なら put を失敗させる (途中で止まったときの再実行のテスト)
        pub fail_put: std::cell::Cell<bool>,
    }

    fn label(store: StoreType) -> &'static str {
        if store == StoreType::Ssm {
            "ssm"
        } else {
            "sm"
        }
    }

    impl FakeStoredValues {
        pub fn get(&self, store: StoreType, name: &str) -> Option<String> {
            let key = (label(store).to_string(), name.to_string());
            self.values.borrow().get(&key).cloned()
        }

        pub fn set(&self, store: StoreType, name: &str, value: &str) {
            let key = (label(store).to_string(), name.to_string());
            self.values.borrow_mut().insert(key, value.to_string());
        }

        pub fn calls(&self) -> Vec<String> {
            self.calls.borrow().clone()
        }
    }

    impl StoredValues for FakeStoredValues {
        async fn put(
            &self,
            name: &str,
            store: StoreType,
            value: &str,
            _region: &str,
        ) -> Result<PutResult> {
            if self.fail_put.get() {
                anyhow::bail!("put failed: {name}");
            }
            self.calls
                .borrow_mut()
                .push(format!("put {} {name}", label(store)));
            let existed = self.get(store, name).is_some();
            self.set(store, name, value);
            Ok(if existed && store == StoreType::Sm {
                PutResult::Updated
            } else {
                PutResult::Created
            })
        }

        async fn read(
            &self,
            name: &str,
            store: StoreType,
            _region: &str,
        ) -> Result<Option<String>> {
            Ok(self.get(store, name))
        }

        async fn delete(&self, name: &str, store: StoreType, _region: &str) -> Result<()> {
            self.calls
                .borrow_mut()
                .push(format!("delete {} {name}", label(store)));
            let key = (label(store).to_string(), name.to_string());
            self.values.borrow_mut().remove(&key);
            Ok(())
        }
    }
}
