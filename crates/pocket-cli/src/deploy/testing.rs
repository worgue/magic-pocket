//! deploy の手順のテストが共有する fake (secret store と管理 API)。

use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};

use anyhow::Result;
use magic_pocket_settings::context::{Context, SecretsContext};
use magic_pocket_settings::model::StoreType;
use serde_json::Value as Json;

use super::mediator::DatabaseUrls;
use super::secret_store::{ManagedSecrets, SecretStore};

/// pocket_key ごとの managed secret と、stored user secret の名前を持つ fake
#[derive(Default)]
pub struct FakeStore {
    pub managed: RefCell<BTreeMap<String, ManagedSecrets>>,
    pub user: BTreeSet<String>,
    pub calls: RefCell<Vec<String>>,
}

impl FakeStore {
    pub fn with(self, pocket_key: &str, secrets: Json) -> Self {
        let Json::Object(map) = secrets else {
            panic!("object が必要です");
        };
        self.managed
            .borrow_mut()
            .insert(pocket_key.to_string(), map);
        self
    }

    pub fn secrets(&self, pocket_key: &str) -> ManagedSecrets {
        self.managed
            .borrow()
            .get(pocket_key)
            .cloned()
            .unwrap_or_default()
    }

    pub fn keys(&self, pocket_key: &str) -> Vec<String> {
        self.secrets(pocket_key).keys().cloned().collect()
    }
}

impl SecretStore for FakeStore {
    async fn read(&self, view: &SecretsContext) -> Result<ManagedSecrets> {
        Ok(self.secrets(&view.pocket_key))
    }

    async fn update(&self, view: &SecretsContext, secrets: &ManagedSecrets) -> Result<()> {
        let keys: Vec<&str> = secrets.keys().map(String::as_str).collect();
        self.calls
            .borrow_mut()
            .push(format!("update {} {}", view.pocket_key, keys.join(",")));
        self.managed
            .borrow_mut()
            .insert(view.pocket_key.clone(), secrets.clone());
        Ok(())
    }

    async fn delete_keys(&self, view: &SecretsContext, keys: &BTreeSet<String>) -> Result<()> {
        let names: Vec<&str> = keys.iter().map(String::as_str).collect();
        self.calls
            .borrow_mut()
            .push(format!("delete {} {}", view.pocket_key, names.join(",")));
        if let Some(stored) = self.managed.borrow_mut().get_mut(&view.pocket_key) {
            for key in keys {
                stored.remove(key);
            }
        }
        Ok(())
    }

    async fn user_secret_exists(
        &self,
        name: &str,
        _store: StoreType,
        _region: &str,
    ) -> Result<bool> {
        Ok(self.user.contains(name))
    }
}

/// 管理 API の fake。`None` は未準備
#[derive(Default)]
pub struct FakeUrls {
    pub neon: Option<String>,
    pub tidb: Option<String>,
    pub upstash: Option<String>,
}

impl DatabaseUrls for FakeUrls {
    async fn neon_database_url(&self, _ctx: &Context) -> Result<Option<String>> {
        Ok(self.neon.clone())
    }
    async fn tidb_database_url(&self, _ctx: &Context) -> Result<Option<String>> {
        Ok(self.tidb.clone())
    }
    async fn upstash_redis_url(&self, _ctx: &Context) -> Result<Option<String>> {
        Ok(self.upstash.clone())
    }
}
