//! `pocket/general_context.py` / `pocket/django/context.py` / `pocket/inbound_context.py`

use serde::Serialize;

use super::{pyformat, sha256_hex_prefix, ContextError};
use crate::model::{
    CacheStore, CloudFront, Container, Django, DjangoCache, DjangoStorage, GeneralSettings,
    Inbound, PublishMode, StorageStore, Versioning, Vpc,
};
use crate::types::{AnyValue, OrderedMap};
use crate::Settings;

#[derive(Debug, Clone, Serialize)]
pub struct EfsContext {
    pub local_mount_path: String,
    pub access_point_path: String,
    pub name: String,
    pub region: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct VpcContext {
    pub r#ref: String,
    pub zone_suffixes: Vec<String>,
    pub nat_gateway: bool,
    pub internet_gateway: bool,
    pub efs: Option<EfsContext>,
    pub name: String,
    pub region: String,
    pub manage: bool,
    pub sharable: bool,
    // computed
    pub private_route_table: bool,
    pub zones: Vec<String>,
}

impl VpcContext {
    pub(crate) fn from_settings(vpc: &Vpc, gs: &GeneralSettings) -> Self {
        let zone_suffixes: Vec<String> = vpc.zone_suffixes.iter().map(|z| z.0.clone()).collect();
        let efs = vpc.efs.as_ref().map(|efs| EfsContext {
            local_mount_path: efs.local_mount_path.0.clone(),
            access_point_path: efs.access_point_path.0.clone(),
            name: format!("{}-{}", vpc.r#ref, gs.namespace),
            region: gs.region.clone(),
        });
        Self {
            r#ref: vpc.r#ref.clone(),
            zones: zone_suffixes
                .iter()
                .map(|s| format!("{}{s}", gs.region))
                .collect(),
            zone_suffixes,
            nat_gateway: vpc.nat_gateway,
            internet_gateway: vpc.internet_gateway,
            efs,
            name: format!("{}-{}", vpc.r#ref, gs.namespace),
            region: gs.region.clone(),
            manage: vpc.manage,
            sharable: vpc.sharable,
            private_route_table: vpc.nat_gateway,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct GeneralContext {
    pub namespace: String,
    pub prefix_template: String,
    pub region: String,
    pub project_name: String,
    pub stages: Vec<String>,
    pub s3_fallback_bucket_name: Option<String>,
    pub django_fallback: Option<DjangoContext>,
}

impl GeneralContext {
    pub(crate) fn from_general_settings(gs: &GeneralSettings) -> Result<Self, ContextError> {
        let django_fallback = DjangoContext::from_settings(&gs.django_fallback, None)?;
        // check_django
        for storage in django_fallback.storages.values() {
            if storage.store == StorageStore::S3 && gs.s3_fallback_bucket_name.is_none() {
                return Err(ContextError::new(
                    "S3 storage is configured in [general.django.storages] but \
                     s3_fallback_bucket_name is not set in [general]. Either add \
                     s3_fallback_bucket_name to [general] for local development, or set \
                     POCKET_STAGE environment variable to use a stage-specific S3 bucket.",
                ));
            }
        }
        Ok(Self {
            namespace: gs.namespace.clone(),
            prefix_template: gs.prefix_template.clone(),
            region: gs.region.clone(),
            project_name: gs.project_name.clone(),
            stages: gs.stages.clone(),
            s3_fallback_bucket_name: gs.s3_fallback_bucket_name.clone(),
            django_fallback: Some(django_fallback),
        })
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct DjangoStorageContext {
    pub store: StorageStore,
    pub location: Option<String>,
    pub r#static: bool,
    pub manifest: bool,
    pub options: OrderedMap<AnyValue>,
    pub distribution: Option<String>,
    pub route: Option<String>,
    pub deploy_hash: bool,
    pub deploy_hash_prefix: Option<String>,
    pub object_hash: bool,
    pub publish: PublishMode,
    pub link: bool,
}

impl DjangoStorageContext {
    fn from_settings(
        storage: &DjangoStorage,
        distributions: Option<&OrderedMap<CloudFront>>,
    ) -> Result<Self, ContextError> {
        if let (Some(distribution), Some(cfs)) = (nonempty(&storage.distribution), distributions) {
            let Some(cf) = cfs.get(distribution) else {
                return Err(ContextError::new(format!(
                    "distribution '{distribution}' not found in cloudfront"
                )));
            };
            if let Some(route) = nonempty(&storage.route) {
                if !cf.routes.iter().any(|r| r.r#ref == route) {
                    return Err(ContextError::new(format!(
                        "route ref '{route}' not found in cloudfront.{distribution}"
                    )));
                }
            }
        }
        let (deploy_hash, deploy_hash_prefix, object_hash) =
            route_versioning(storage, distributions)?;
        Ok(Self {
            store: storage.store,
            location: storage.location.clone(),
            r#static: storage.r#static,
            manifest: storage.manifest,
            options: storage.options.clone(),
            distribution: storage.distribution.clone(),
            route: storage.route.clone(),
            deploy_hash,
            deploy_hash_prefix,
            object_hash,
            publish: storage.publish,
            link: storage.link,
        })
    }
}

/// storage が指す route の (deploy_hash か, その prefix, object_hash か)
fn route_versioning(
    storage: &DjangoStorage,
    distributions: Option<&OrderedMap<CloudFront>>,
) -> Result<(bool, Option<String>, bool), ContextError> {
    let (Some(distribution), Some(route), Some(cfs)) = (
        nonempty(&storage.distribution),
        nonempty(&storage.route),
        distributions,
    ) else {
        return Ok((false, None, false));
    };
    let Some(cf) = cfs.get(distribution) else {
        return Ok((false, None, false));
    };
    let Some(r) = cf.routes.iter().find(|r| r.r#ref == route) else {
        return Ok((false, None, false));
    };
    match r.versioning {
        Some(Versioning::DeployHash) => {
            let prefix = r.path_pattern.trim_end_matches('*').trim_end_matches('/');
            Ok((true, Some(format!("{prefix}/")), false))
        }
        Some(Versioning::ObjectHash) => {
            if !storage.r#static {
                return Err(ContextError::new(format!(
                    "object_hash の route '{route}' は static = true の storage でだけ使えます \
                     (URL の解決だけを行い、保存はできません)"
                )));
            }
            Ok((false, None, true))
        }
        _ => Ok((false, None, false)),
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct DjangoCacheContext {
    pub store: CacheStore,
    pub location_subdir: String,
    pub location: Option<String>,
}

impl DjangoCacheContext {
    fn from_settings(
        cache: &DjangoCache,
        owner: Option<(&Settings, &Container)>,
    ) -> Result<Self, ContextError> {
        let location = match cache.store {
            CacheStore::Locmem | CacheStore::Redis => None,
            CacheStore::Efs => {
                let efs = owner.and_then(|(root, c)| Some((root, c.vpc.as_ref()?.efs.as_ref()?)));
                let Some((root, efs)) = efs else {
                    return Err(ContextError::new("efs cache requires container.vpc.efs"));
                };
                let joined = path_join(&efs.local_mount_path.0, &cache.location_subdir);
                Some(pyformat(
                    &joined,
                    &[
                        ("namespace", root.general.namespace.as_str()),
                        ("stage", root.stage.as_str()),
                        ("project", root.project_name()),
                    ],
                ))
            }
        };
        Ok(Self {
            store: cache.store,
            location_subdir: cache.location_subdir.clone(),
            location,
        })
    }
}

/// `str(Path(base) / child)` (POSIX の pathlib と同じ正規化)
fn path_join(base: &str, child: &str) -> String {
    let joined = if child.starts_with('/') {
        child.to_string()
    } else {
        format!("{base}/{child}")
    };
    let absolute = joined.starts_with('/');
    let parts: Vec<&str> = joined
        .split('/')
        .filter(|p| !p.is_empty() && *p != ".")
        .collect();
    let body = parts.join("/");
    match (absolute, body.is_empty()) {
        (true, _) => format!("/{body}"),
        (false, true) => ".".to_string(),
        (false, false) => body,
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct DjangoContext {
    pub storages: OrderedMap<DjangoStorageContext>,
    pub caches: OrderedMap<DjangoCacheContext>,
    pub settings: OrderedMap<AnyValue>,
    pub project_dir: Option<String>,
}

impl DjangoContext {
    /// `owner` は container の django のとき (root, container)。general の
    /// django_fallback では None
    pub(crate) fn from_settings(
        django: &Django,
        owner: Option<(&Settings, &Container)>,
    ) -> Result<Self, ContextError> {
        let distributions = owner
            .map(|(root, _)| &root.cloudfront)
            .filter(|cfs| !cfs.is_empty());
        let mut storages = OrderedMap::default();
        for (key, storage) in django.storages.iter().flat_map(|s| s.iter()) {
            storages.insert(
                key.clone(),
                DjangoStorageContext::from_settings(storage, distributions)?,
            );
        }
        let mut caches = OrderedMap::default();
        for (key, cache) in django.caches.iter().flat_map(|c| c.iter()) {
            caches.insert(
                key.clone(),
                DjangoCacheContext::from_settings(cache, owner)?,
            );
        }
        Ok(Self {
            storages,
            caches,
            settings: django.settings.clone(),
            project_dir: django.project_dir.clone(),
        })
    }
}

/// AWS General Reference「Email Receiving endpoints」(Python の `RECEIVING_REGIONS`)
const RECEIVING_REGIONS: &[&str] = &[
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
    "af-south-1",
    "ap-southeast-3",
    "ap-south-1",
    "ap-northeast-3",
    "ap-northeast-2",
    "ap-southeast-1",
    "ap-southeast-2",
    "ap-northeast-1",
    "ca-central-1",
    "eu-central-1",
    "eu-west-1",
    "eu-west-2",
    "eu-south-1",
    "eu-west-3",
    "eu-north-1",
    "il-central-1",
    "me-south-1",
    "sa-east-1",
];

/// `re.sub("[^a-z0-9-]", "-", s)`
fn dashify(s: &str) -> String {
    s.chars()
        .map(|c| {
            if c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-' {
                c
            } else {
                '-'
            }
        })
        .collect()
}

#[derive(Debug, Clone, Serialize)]
pub struct InboundRuntimeConfig {
    pub region: String,
    pub bucket: String,
    pub topic_arn: String,
    pub raw_prefix: String,
    pub metadata_prefix: String,
    pub import_prefix: String,
    pub recipients: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct InboundContext {
    pub name: String,
    pub region: String,
    pub resource_name: String,
    pub queue_name: String,
    pub config: Inbound,
    // computed
    pub bucket_name: String,
    pub topic_arn: String,
    pub queue_arn: String,
    pub runtime_config: InboundRuntimeConfig,
}

impl InboundContext {
    pub(crate) fn from_settings(name: &str, root: &Settings) -> Result<Self, ContextError> {
        let region = root.general.region.as_str();
        if !RECEIVING_REGIONS.contains(&region) {
            return Err(ContextError::new("SES受信対応リージョンを指定してください"));
        }
        let config = root
            .inbound
            .get(name)
            .expect("inbound の名前は settings から取る")
            .clone();
        let (container, handler) = parse_handler_ref(&config.handler)?;
        let base = format!("{}-{}-inbound-{name}", root.slug(), root.general.namespace);
        let digest = sha256_hex_prefix(&format!("{base}-{region}"), 10);
        let resource_name = format!("{}-{digest}", truncate_chars(&dashify(&base), 32));
        let prefix = pyformat(&root.general.prefix_template, &format_vars(root));
        let bucket_name = format!("{resource_name}-${{AWS::AccountId}}");
        let topic_arn =
            format!("arn:${{AWS::Partition}}:sns:{region}:${{AWS::AccountId}}:{resource_name}");
        let queue_name = format!("{prefix}{container}-{handler}");
        let queue_arn =
            format!("arn:${{AWS::Partition}}:sqs:{region}:${{AWS::AccountId}}:{queue_name}");
        let runtime_config = InboundRuntimeConfig {
            region: region.to_string(),
            bucket: bucket_name.clone(),
            topic_arn: topic_arn.clone(),
            raw_prefix: config.raw_prefix.clone(),
            metadata_prefix: config.metadata_prefix.clone(),
            import_prefix: config.import_prefix.clone(),
            recipients: config.recipients.clone(),
        };
        Ok(Self {
            name: name.to_string(),
            region: region.to_string(),
            resource_name,
            queue_name,
            config,
            bucket_name,
            topic_arn,
            queue_arn,
            runtime_config,
        })
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct InboundDomainContext {
    pub domain: String,
    pub region: String,
    pub stack_name: String,
    pub manage_dns: bool,
    pub hosted_zone_id_override: Option<String>,
    // computed
    pub mx_value: String,
}

impl InboundDomainContext {
    pub(crate) fn from_settings(root: &Settings) -> OrderedMap<Self> {
        let region = root.general.region.as_str();
        let base = dashify(&format!(
            "{}-{}-inbound-domain",
            root.slug(),
            root.general.namespace
        ));
        let mut names: Vec<&String> = root.inbound.keys().collect();
        names.sort();
        let mut domains = OrderedMap::default();
        for name in names {
            let config = root.inbound.get(name).expect("inbound");
            if domains.contains_key(&config.domain) {
                continue;
            }
            let digest = sha256_hex_prefix(&format!("{base}-{region}-{}", config.domain), 10);
            domains.insert(
                config.domain.clone(),
                Self {
                    domain: config.domain.clone(),
                    region: region.to_string(),
                    stack_name: format!("{}-{digest}", truncate_chars(&base, 32)),
                    manage_dns: config.manage_dns,
                    hosted_zone_id_override: config.hosted_zone_id_override.clone(),
                    mx_value: format!("10 inbound-smtp.{region}.amazonaws.com"),
                },
            );
        }
        domains
    }
}

/// Python の truthy な文字列 (`None` と空文字を除く)
fn nonempty(s: &Option<String>) -> Option<&str> {
    s.as_deref().filter(|s| !s.is_empty())
}

fn truncate_chars(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// `Settings.format_vars`
pub(crate) fn format_vars(root: &Settings) -> [(&str, &str); 3] {
    [
        ("namespace", root.general.namespace.as_str()),
        ("stage", root.stage.as_str()),
        ("project", root.project_name()),
    ]
}

/// `pocket.settings.parse_handler_ref`
pub(crate) fn parse_handler_ref(reference: &str) -> Result<(String, String), ContextError> {
    match reference.split_once('.') {
        Some((c, h)) => Ok((c.to_string(), h.to_string())),
        None => Err(ContextError::new(format!(
            "handler 参照 '{reference}' には container 名が必要です。 \
             \"<container>.<handler>\" のドット記法で指定してください \
             (例: handler = \"mydjango.wsgi\")。"
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_path_join_matches_pathlib() {
        assert_eq!(path_join("/mnt/efs", "{stage}"), "/mnt/efs/{stage}");
        assert_eq!(path_join("/mnt/efs/", "a//b/"), "/mnt/efs/a/b");
        assert_eq!(path_join("/mnt/efs", "/abs"), "/abs");
        assert_eq!(path_join("/mnt/efs", "./x"), "/mnt/efs/x");
    }
}
