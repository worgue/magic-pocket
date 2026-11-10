//! `pocket/django/settings.py` の `Django` / `DjangoStorage` / `DjangoCache`。

use serde::{Deserialize, Serialize};

use crate::error::{child, PathSeg, SettingsError};
use crate::types::{AnyValue, OrderedMap};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum StorageStore {
    S3,
    Filesystem,
}

/// staticfiles の publish 方式 (Python の `PublishMode`)
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum PublishMode {
    #[default]
    Deploy,
    Command,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DjangoStorage {
    pub store: StorageStore,
    #[serde(default)]
    pub location: Option<String>,
    #[serde(default)]
    pub r#static: bool,
    #[serde(default)]
    pub manifest: bool,
    #[serde(default)]
    pub options: OrderedMap<AnyValue>,
    #[serde(default)]
    pub distribution: Option<String>,
    #[serde(default)]
    pub route: Option<String>,
    #[serde(default)]
    pub publish: PublishMode,
    #[serde(default)]
    pub link: bool,
}

impl DjangoStorage {
    pub fn new(store: StorageStore, is_static: bool) -> Self {
        Self {
            store,
            location: None,
            r#static: is_static,
            manifest: false,
            options: OrderedMap::default(),
            distribution: None,
            route: None,
            publish: PublishMode::Deploy,
            link: false,
        }
    }

    /// Python の validator を宣言順に
    fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        let err = |m: &str| Err(SettingsError::value(path.to_vec(), m));
        // check_manifest
        if self.manifest && !self.r#static {
            return err("manifest can only be used with static");
        }
        // check_location
        if self.store == StorageStore::S3 && self.location.is_none() && self.distribution.is_none()
        {
            return err("location is required for s3 storage without distribution");
        }
        if self.distribution.is_some() && self.location.is_some() {
            return err("location cannot be used with distribution \
                 (S3 location is computed from the route's origin_path)");
        }
        // check_route
        if self.route.is_some() && self.distribution.is_none() {
            return err("route requires distribution");
        }
        // check_distribution
        if self.distribution.is_some() && self.store != StorageStore::S3 {
            return err("distribution can only be used with s3 store");
        }
        // check_publish
        if self.publish != PublishMode::Deploy && !self.r#static {
            return err("publish can only be used with static storage");
        }
        // check_link
        if self.link && !self.r#static {
            return err("link can only be used with static storage");
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum CacheStore {
    Efs,
    Locmem,
    Redis,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DjangoCache {
    pub store: CacheStore,
    #[serde(default = "default_location_subdir")]
    pub location_subdir: String,
}

fn default_location_subdir() -> String {
    "{stage}".to_string()
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Django {
    /// validate 後は必ず Some (Python の `set_defaults` と同じく既定を補う)
    #[serde(default)]
    pub storages: Option<OrderedMap<DjangoStorage>>,
    #[serde(default)]
    pub caches: Option<OrderedMap<DjangoCache>>,
    #[serde(default)]
    pub settings: OrderedMap<AnyValue>,
    #[serde(default)]
    pub project_dir: Option<String>,
}

impl Default for Django {
    /// Python の `Django()` (validator を通した後の既定)
    fn default() -> Self {
        let mut d = Self {
            storages: None,
            caches: None,
            settings: OrderedMap::default(),
            project_dir: None,
        };
        d.validate(&[]).expect("既定の Django 設定は検証を通る");
        d
    }
}

impl Django {
    pub(crate) fn validate(&mut self, path: &[PathSeg]) -> Result<(), SettingsError> {
        // field の検証 (入れ子のモデル) が先、モデルの validator が後 = pydantic と同じ順
        if let Some(storages) = &self.storages {
            for (key, s) in storages.iter() {
                s.validate(&child(&child(path, "storages"), key))?;
            }
        }
        // set_defaults
        let err = |m: &str| Err(SettingsError::value(path.to_vec(), m));
        let storages = self.storages.get_or_insert_with(|| {
            OrderedMap(vec![
                (
                    "default".to_string(),
                    DjangoStorage::new(StorageStore::Filesystem, false),
                ),
                (
                    "staticfiles".to_string(),
                    DjangoStorage::new(StorageStore::Filesystem, true),
                ),
            ])
        });
        let Some(staticfiles) = storages.get("staticfiles") else {
            return err("staticfiles storage is required");
        };
        if !storages.contains_key("default") {
            return err("default storage is required");
        }
        if !staticfiles.r#static {
            return err("staticfiles storage must be static");
        }
        if storages
            .iter()
            .any(|(key, s)| key != "staticfiles" && s.r#static)
        {
            return err("static can only be used with staticfiles");
        }
        if self.caches.is_none() {
            self.caches = Some(OrderedMap(vec![(
                "default".to_string(),
                DjangoCache {
                    store: CacheStore::Locmem,
                    location_subdir: default_location_subdir(),
                },
            )]));
        }
        Ok(())
    }
}
