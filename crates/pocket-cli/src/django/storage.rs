//! Django の storage の解決 (Python 版 `pocket/django/utils.py` の `get_storages` /
//! `get_static_storage_s3_options` と `pocket_cli/django_cli.py` の
//! `get_deploystatic_local_storage`)。
//!
//! storage は pocket.toml 由来の Context (と `GeneralContext`) だけで決まり、Django の設定は
//! 読まない。ここでは upload に要る値 (BACKEND・bucket・location) だけを写す
//! (signer・custom_domain 等の配信用の値は写さない)。

use std::path::{Path, PathBuf};

use anyhow::{anyhow, bail, Result};
use magic_pocket_settings::context::{
    ContainerContext, Context, DjangoContext, DjangoStorageContext, GeneralContext,
};
use magic_pocket_settings::model::{PublishMode, StorageStore};
use serde_json::{json, Map, Value as Json};

pub const STATIC_FILES_STORAGE: &str = "django.contrib.staticfiles.storage.StaticFilesStorage";
pub const MANIFEST_STATIC_FILES_STORAGE: &str =
    "django.contrib.staticfiles.storage.ManifestStaticFilesStorage";
pub const FILE_SYSTEM_STORAGE: &str = "django.core.files.storage.FileSystemStorage";
pub const S3_BOTO3_STORAGE: &str = "storages.backends.s3boto3.S3Boto3Storage";

/// Python の `resolve_django_container`: django 設定を持つ container (無ければ `None`)。
///
/// `env_name` は環境変数 `POCKET_CONTAINER`
pub fn resolve_django_container<'c>(
    context: &'c Context,
    env_name: Option<&str>,
) -> Result<Option<&'c ContainerContext>> {
    if let Some(own) = env_name
        .filter(|name| !name.is_empty())
        .and_then(|name| context.container.get(name))
    {
        return Ok(own.django.as_ref().map(|_| own));
    }
    let candidates: Vec<(&String, &ContainerContext)> = context
        .container
        .iter()
        .filter(|(_, c)| c.django.is_some())
        .collect();
    match candidates.as_slice() {
        [] => Ok(None),
        [(_, only)] => Ok(Some(only)),
        _ => {
            let mut names: Vec<&str> = candidates.iter().map(|(n, _)| n.as_str()).collect();
            names.sort();
            bail!(
                "django 設定を持つ container が複数あります ({})。POCKET_CONTAINER 環境変数か \
                 --container で対象を指定してください。",
                names.join(", ")
            )
        }
    }
}

/// 環境変数 `POCKET_CONTAINER`
pub fn env_container() -> Option<String> {
    std::env::var("POCKET_CONTAINER").ok()
}

/// stage の container の staticfiles の宣言 (container の django に無ければ `None`)。
///
/// Python 版の `_staticfiles_publish_mode` / `_staticfiles_link` と同じく、general の
/// django_fallback は見ない
pub fn container_staticfiles<'c>(
    context: &'c Context,
    env_name: Option<&str>,
) -> Result<Option<&'c DjangoStorageContext>> {
    Ok(resolve_django_container(context, env_name)?
        .and_then(|c| c.django.as_ref())
        .and_then(|django| django.storages.get("staticfiles")))
}

/// staticfiles の publish 方式 (Python の `_staticfiles_publish_mode`)
pub fn staticfiles_publish_mode(context: &Context, env_name: Option<&str>) -> Result<PublishMode> {
    Ok(container_staticfiles(context, env_name)?.map_or(PublishMode::Deploy, |s| s.publish))
}

/// staticfiles の `link` (Python の `_staticfiles_link`)
pub fn staticfiles_link(context: &Context, env_name: Option<&str>) -> Result<bool> {
    Ok(container_staticfiles(context, env_name)?.is_some_and(|s| s.link))
}

/// staticfiles の route が `versioning = "object_hash"` か (`pocket-rs static publish` で上げる)
pub fn staticfiles_object_hash(context: &Context, env_name: Option<&str>) -> Result<bool> {
    Ok(container_staticfiles(context, env_name)?.is_some_and(|s| s.object_hash))
}

/// Python の `resolve_django_context(require="storages")`: stage の container の django
/// (storages を持つとき) か、general の django_fallback
pub fn storage_django_context<'c>(
    general: &'c GeneralContext,
    context: Option<&'c Context>,
    env_name: Option<&str>,
) -> Result<&'c DjangoContext> {
    let fallback = general
        .django_fallback
        .as_ref()
        .ok_or_else(|| anyhow!("Never happen because of context validation."))?;
    let container = match context {
        Some(context) => resolve_django_container(context, env_name)?,
        None => None,
    };
    match container.and_then(|c| c.django.as_ref()) {
        Some(django) if !django.storages.is_empty() => Ok(django),
        _ => Ok(fallback),
    }
}

/// `DjangoStorageContext.backend`
pub fn backend(storage: &DjangoStorageContext) -> &'static str {
    if storage.deploy_hash && storage.r#static {
        return STATIC_FILES_STORAGE;
    }
    if storage.object_hash {
        return "pocket.django.storages.ObjectHashStaticStorage";
    }
    let distribution = storage
        .distribution
        .as_deref()
        .is_some_and(|d| !d.is_empty());
    match storage.store {
        StorageStore::S3 if distribution => match (storage.r#static, storage.manifest) {
            (true, true) => "pocket.django.storages.CloudFrontS3ManifestStaticStorage",
            (true, false) => "pocket.django.storages.CloudFrontS3StaticStorage",
            _ => "pocket.django.storages.CloudFrontS3Boto3Storage",
        },
        StorageStore::S3 => match (storage.r#static, storage.manifest) {
            (true, true) => "storages.backends.s3boto3.S3ManifestStaticStorage",
            (true, false) => "storages.backends.s3boto3.S3StaticStorage",
            _ => S3_BOTO3_STORAGE,
        },
        StorageStore::Filesystem => match (storage.r#static, storage.manifest) {
            (true, true) => MANIFEST_STATIC_FILES_STORAGE,
            (true, false) => STATIC_FILES_STORAGE,
            _ => FILE_SYSTEM_STORAGE,
        },
    }
}

/// S3 の置き場
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct S3Place {
    pub bucket_name: String,
    /// bucket 内の prefix (前後に `/` は付かない。空なら bucket の直下)
    pub location: String,
    /// bucket の region
    pub region: String,
}

/// storage の OPTIONS のうち upload に要る値
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StorageOptions {
    S3(S3Place),
    Filesystem { location: Option<String> },
}

/// Python の `_build_storage_options` (bucket_name と location だけ)
pub fn build_storage_options(
    storage: &DjangoStorageContext,
    context: Option<&Context>,
    general: &GeneralContext,
) -> Result<StorageOptions> {
    match storage.store {
        StorageStore::Filesystem => Ok(StorageOptions::Filesystem {
            location: storage.location.clone(),
        }),
        StorageStore::S3 => {
            if let Some(distribution) = storage.distribution.as_deref().filter(|d| !d.is_empty()) {
                let context = context
                    .ok_or_else(|| anyhow!("context is required for distribution storage"))?;
                let cf = context
                    .cloudfront
                    .get(distribution)
                    .ok_or_else(|| anyhow!("KeyError: '{distribution}'"))?;
                let route = match storage.route.as_deref().filter(|r| !r.is_empty()) {
                    Some(r) => cf
                        .routes
                        .iter()
                        .find(|route| route.r#ref == r)
                        .ok_or_else(|| anyhow!("route ref '{r}' not found"))?,
                    None => &cf.default_route,
                };
                return Ok(StorageOptions::S3(S3Place {
                    bucket_name: cf.bucket_name.clone(),
                    location: crate::deploy::upload::s3_prefix(route),
                    region: cf.s3_region.clone(),
                }));
            }
            // S3 直接 (ローカル開発用)
            let (bucket_name, region) = match context {
                Some(context) => {
                    let s3 = context
                        .s3
                        .as_ref()
                        .ok_or_else(|| anyhow!("Never happen because of context validation."))?;
                    (s3.bucket_name.clone(), s3.region.clone())
                }
                None => {
                    let Some(bucket) = general.s3_fallback_bucket_name.clone() else {
                        bail!(
                            "S3 storage is configured but s3_fallback_bucket_name is not set in \
                             [general]. Add it for local development, or set POCKET_STAGE."
                        );
                    };
                    (bucket, general.region.clone())
                }
            };
            Ok(StorageOptions::S3(S3Place {
                bucket_name,
                location: storage.location.clone().unwrap_or_default(),
                region,
            }))
        }
    }
}

/// `get_storages` の 1 件 (BACKEND と upload に要る OPTIONS)
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedStorage {
    pub backend: &'static str,
    /// S3 の置き場を持たない storage (deploy_hash の staticfiles・object_hash) は `None`
    pub options: Option<StorageOptions>,
}

impl ResolvedStorage {
    /// Python の `get_storages()[key]` のうち BACKEND・bucket_name・location
    pub fn to_json(&self) -> Json {
        let mut value = Map::new();
        value.insert("BACKEND".into(), json!(self.backend));
        match &self.options {
            Some(StorageOptions::S3(place)) => {
                value.insert(
                    "OPTIONS".into(),
                    json!({"bucket_name": place.bucket_name, "location": place.location}),
                );
            }
            Some(StorageOptions::Filesystem {
                location: Some(location),
            }) => {
                value.insert("OPTIONS".into(), json!({ "location": location }));
            }
            Some(StorageOptions::Filesystem { location: None }) | None => {}
        }
        Json::Object(value)
    }
}

/// storage の `options` の文字列の値 (Python 版は OPTIONS に上書きで merge する)
fn option_str(storage: &DjangoStorageContext, key: &str) -> Option<String> {
    storage
        .options
        .get(key)
        .and_then(|v| v.0.as_str())
        .map(str::to_string)
}

/// Python の `get_storages(stage=…)`。`context` が `None` なら stage 無し (general の
/// django_fallback)。`POCKET_STATICFILES_BACKEND_OVERRIDE` は見ない (collectstatic の
/// 子 process にだけ渡す値)
pub fn get_storages(
    general: &GeneralContext,
    context: Option<&Context>,
    env_name: Option<&str>,
) -> Result<Vec<(String, ResolvedStorage)>> {
    let django = storage_django_context(general, context, env_name)?;
    let mut storages = Vec::new();
    for (key, storage) in django.storages.iter() {
        let backend = backend(storage);
        let options = if (storage.deploy_hash && storage.r#static) || storage.object_hash {
            None
        } else {
            match build_storage_options(storage, context, general)? {
                StorageOptions::S3(mut place) => {
                    if let Some(bucket) = option_str(storage, "bucket_name") {
                        place.bucket_name = bucket;
                    }
                    if let Some(location) = option_str(storage, "location") {
                        place.location = location;
                    }
                    Some(StorageOptions::S3(place))
                }
                StorageOptions::Filesystem { location } => Some(StorageOptions::Filesystem {
                    location: option_str(storage, "location").or(location),
                }),
            }
        };
        storages.push((key.clone(), ResolvedStorage { backend, options }));
    }
    Ok(storages)
}

/// Python の `get_static_storage_s3_options`: staticfiles の S3 の置き場
pub fn static_storage_s3_place(
    general: &GeneralContext,
    context: &Context,
    env_name: Option<&str>,
) -> Result<S3Place> {
    let django = storage_django_context(general, Some(context), env_name)?;
    let storage = django
        .storages
        .get("staticfiles")
        .filter(|s| s.store == StorageStore::S3)
        .ok_or_else(|| anyhow!("deploystatic requires staticfiles with store = 's3'"))?;
    match build_storage_options(storage, Some(context), general)? {
        StorageOptions::S3(place) => Ok(place),
        StorageOptions::Filesystem { .. } => bail!("Failed to build S3 options for staticfiles"),
    }
}

/// collectstatic のローカルの backend と出力先
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LocalStaticBuild {
    pub backend: &'static str,
    pub location: PathBuf,
}

/// Python の `get_deploystatic_local_storage`
pub fn local_static_build(
    storages: &[(String, ResolvedStorage)],
    project_dir: &Path,
    stage: &str,
) -> Result<LocalStaticBuild> {
    let Some((_, storage)) = storages.iter().find(|(key, _)| key == "staticfiles") else {
        bail!("staticfiles storage not found in the stage {stage}");
    };
    let backend = match storage.backend {
        STATIC_FILES_STORAGE | MANIFEST_STATIC_FILES_STORAGE => storage.backend,
        "storages.backends.s3boto3.S3StaticStorage"
        | "pocket.django.storages.CloudFrontS3StaticStorage" => STATIC_FILES_STORAGE,
        "storages.backends.s3boto3.S3ManifestStaticStorage"
        | "pocket.django.storages.CloudFrontS3ManifestStaticStorage" => {
            MANIFEST_STATIC_FILES_STORAGE
        }
        other => bail!("BACKEND {other} is not supported"),
    };
    Ok(LocalStaticBuild {
        backend,
        location: project_dir
            .join("pocket_cache")
            .join("static_build")
            .join(stage),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stack::testing::vector_context;

    fn general(ctx: &Context) -> &GeneralContext {
        ctx.general.as_ref().expect("general")
    }

    #[test]
    fn test_local_static_build_maps_backends() {
        let storage = |backend| {
            vec![(
                "staticfiles".to_string(),
                ResolvedStorage {
                    backend,
                    options: None,
                },
            )]
        };
        let dir = Path::new("/prj");
        for (given, local) in [
            (STATIC_FILES_STORAGE, STATIC_FILES_STORAGE),
            (MANIFEST_STATIC_FILES_STORAGE, MANIFEST_STATIC_FILES_STORAGE),
            (
                "storages.backends.s3boto3.S3StaticStorage",
                STATIC_FILES_STORAGE,
            ),
            (
                "pocket.django.storages.CloudFrontS3ManifestStaticStorage",
                MANIFEST_STATIC_FILES_STORAGE,
            ),
        ] {
            let built = local_static_build(&storage(given), dir, "dev").expect("build");
            assert_eq!(built.backend, local);
            assert_eq!(
                built.location,
                PathBuf::from("/prj/pocket_cache/static_build/dev")
            );
        }
        let error = local_static_build(
            &storage("pocket.django.storages.ObjectHashStaticStorage"),
            dir,
            "dev",
        )
        .expect_err("object_hash");
        assert_eq!(
            error.to_string(),
            "BACKEND pocket.django.storages.ObjectHashStaticStorage is not supported"
        );
        let error = local_static_build(&[], dir, "dev").expect_err("無い");
        assert_eq!(
            error.to_string(),
            "staticfiles storage not found in the stage dev"
        );
    }

    #[test]
    fn test_container_vector_resolves_storages() {
        // 照合は tests/test_django_storages_rs.py (Python の get_storages と比べる)。
        // ここでは stage 無しの解決が general の django_fallback になることだけを見る
        let ctx = vector_context("toml_vectors/context_full.dev");
        let fallback = get_storages(general(&ctx), None, None).expect("fallback");
        assert!(fallback.iter().any(|(key, _)| key == "staticfiles"));
    }
}
