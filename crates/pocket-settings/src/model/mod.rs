//! 設定モデル本体。Python の `pocket.settings.Settings` と同じ形・同じ検証。

pub mod cloudfront;
pub mod container;
pub mod db;
pub mod django;
pub mod general;
pub mod misc;
pub mod provisioning;
pub mod s3;

use serde::{Deserialize, Serialize};

use crate::error::{path, SettingsError};
use std::sync::OnceLock;

use regex::Regex;

use crate::error::child;
use crate::types::{OrderedMap, TagStr};

pub use cloudfront::{
    CloudFront, CloudFrontWaf, CloudFrontWafAllowRule, CloudFrontWafRateLimit, RateLimitAction,
    RedirectFrom, Route, RouteBuild, RouteType, StaticSource, Versioning,
};
pub use container::{
    Alert, ApiGateway, BuildBackend, BuildConfig, Container, ContainerIam, LambdaHandler,
    ManagedSecretSpec, ManagedSecretType, Platform, Secrets, Sqs, StoreType, UserSecretSpec,
    UserSecretType,
};
pub use db::{
    Backup, Dsql, DsqlBackupPlan, DsqlDailyRule, DsqlMonthlyRule, DsqlWeeklyRule, PasswordStrategy,
    Rds, RdsBackup, RdsBackupPlan, RdsMonthlyRule, RdsWeeklyRule,
};
pub use django::{CacheStore, Django, DjangoCache, DjangoStorage, PublishMode, StorageStore};
pub use general::{Efs, GeneralSettings, Iam, Vpc};
pub use misc::{Inbound, ScheduleEntry, Scheduler, SchedulerKind, Ses, TlsPolicy};
pub use provisioning::{Neon, ProvisioningMode, TiDb, Upstash};
pub use s3::{OneOrMany, S3Cors, S3LifecycleRule, S3};

/// pocket.toml の top-level に書ける key (Python の `Settings.model_fields` から
/// `stage` を除いたもの)。stage 上書きの table と区別する前処理で使う
pub(crate) const TOP_LEVEL_KEYS: &[&str] = &[
    "general",
    "vpc",
    "container",
    "neon",
    "tidb",
    "upstash",
    "dsql",
    "rds",
    "backup",
    "ses",
    "inbound",
    "s3",
    "cloudfront",
    "scheduler",
    "iam",
];

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Settings {
    pub general: GeneralSettings,
    pub stage: TagStr,
    #[serde(default)]
    pub vpc: Option<Vpc>,
    #[serde(default)]
    pub container: OrderedMap<Container>,
    #[serde(default)]
    pub neon: Option<Neon>,
    #[serde(default)]
    pub tidb: Option<TiDb>,
    #[serde(default)]
    pub upstash: Option<Upstash>,
    #[serde(default)]
    pub dsql: Option<Dsql>,
    #[serde(default)]
    pub rds: Option<Rds>,
    #[serde(default)]
    pub backup: Option<Backup>,
    #[serde(default)]
    pub ses: Option<Ses>,
    #[serde(default)]
    pub inbound: OrderedMap<Inbound>,
    #[serde(default)]
    pub s3: Option<S3>,
    #[serde(default)]
    pub cloudfront: OrderedMap<CloudFront>,
    #[serde(default)]
    pub scheduler: Option<Scheduler>,
    #[serde(default)]
    pub iam: Iam,
}

impl Settings {
    pub fn project_name(&self) -> &str {
        &self.general.project_name
    }

    /// 環境の識別子。e.g) dev-myprj (Python の computed field `slug`)
    pub fn slug(&self) -> String {
        format!("{}-{}", self.stage.as_str(), self.general.project_name)
    }

    /// Python の `model_dump(mode="json")` と同じ形の JSON
    /// (全 field を出し、`None` は `null`、computed field も出す)
    pub fn to_json(&self) -> serde_json::Value {
        let mut value = serde_json::to_value(self).expect("Settings は JSON にできる");
        let obj = value.as_object_mut().expect("Settings は object");
        obj.insert("slug".to_string(), serde_json::Value::String(self.slug()));
        value
    }

    /// Python の validator を宣言順に (入れ子のモデルが先)
    pub(crate) fn validate(&mut self) -> Result<(), SettingsError> {
        self.general.validate(&path(&["general"]))?;
        if let Some(vpc) = &self.vpc {
            vpc.validate(&path(&["vpc"]))?;
        }
        let containers = path(&["container"]);
        for (name, c) in self.container.iter_mut() {
            c.validate(&child(&containers, name))?;
        }
        if let Some(rds) = &self.rds {
            rds.validate(&path(&["rds"]))?;
        }
        if let Some(backup) = &self.backup {
            backup.validate(&path(&["backup"]))?;
        }
        let inbounds = path(&["inbound"]);
        for (name, inlet) in self.inbound.iter() {
            inlet.validate(&child(&inbounds, name))?;
        }
        if let Some(s3) = &self.s3 {
            s3.validate(&path(&["s3"]))?;
        }
        let cloudfronts = path(&["cloudfront"]);
        for (name, cf) in self.cloudfront.iter() {
            cf.validate(&child(&cloudfronts, name))?;
        }
        if let Some(scheduler) = &self.scheduler {
            scheduler.validate(&path(&["scheduler"]))?;
        }
        self.check_container_names()?;
        self.check_container_secrets_consistency()?;
        self.check_inbound_handlers()?;
        self.check_backup_targets()?;
        self.check_rds_vpc()?;
        self.check_rds_database_url_requires_vpc()?;
        self.check_scheduler_handlers()?;
        self.check_cloudfront_requires_s3()?;
        Ok(())
    }

    /// 検証は通ったが知らせたいこと (Python の `_emit_advisories`)
    pub(crate) fn advisories(&self) -> Vec<crate::Advisory> {
        let mut out = Vec::new();
        for cf in self.cloudfront.values() {
            for route in &cf.routes {
                if let Some(message) = route.double_prefix_advisory() {
                    out.push(crate::Advisory { message });
                }
            }
        }
        if self.rds.is_some() {
            for (name, c) in self.container.iter() {
                if c.vpc.is_none() {
                    out.push(crate::Advisory {
                        message: format!(
                            "container.{name} は use_vpc=false のため、この stage の [rds] \
                             (private subnet 内) に到達できません。この container には \
                             POCKET_RDS_* / DATABASE_URL (RDS) を注入しません。この container \
                             が RDS を使わないなら問題ありません"
                        ),
                    });
                }
            }
        }
        out
    }

    fn check_inbound_handlers(&self) -> Result<(), SettingsError> {
        let err = |m: String| Err(SettingsError::value(Vec::new(), m));
        static RE: OnceLock<Regex> = OnceLock::new();
        let name_re = RE.get_or_init(|| Regex::new("^[a-z][a-z0-9]{0,31}$").expect("re"));
        let mut handlers: Vec<&str> = Vec::new();
        let mut recipients: Vec<&str> = Vec::new();
        let mut dns: Vec<(&str, (bool, Option<&str>))> = Vec::new();
        for (name, inlet) in self.inbound.iter() {
            if !inlet.manage_dns
                && inlet
                    .hosted_zone_id_override
                    .as_deref()
                    .is_some_and(|z| !z.is_empty())
            {
                return err(
                    "manage_dns = false では hosted_zone_id_override を指定できません".to_string(),
                );
            }
            let declared = (inlet.manage_dns, inlet.hosted_zone_id_override.as_deref());
            match dns.iter().find(|(d, _)| *d == inlet.domain) {
                Some((_, first)) if *first != declared => return err(
                    "同じdomainのinboundは manage_dns と hosted_zone_id_override を揃えてください"
                        .to_string(),
                ),
                Some(_) => {}
                None => dns.push((&inlet.domain, declared)),
            }
            if !name_re.is_match(name) {
                return err("inbound名はcontainer名と同じ形式で指定してください".to_string());
            }
            let (container, _, handler) = match self.resolve_handler(&inlet.handler) {
                Ok(r) => r,
                Err(m) => return err(m),
            };
            if !handler
                .sqs
                .as_ref()
                .is_some_and(|q| q.report_batch_item_failures)
            {
                return err(
                    "inbound.handlerにはpartial batch response対応SQS workerが必要です".to_string(),
                );
            }
            if handlers.contains(&inlet.handler.as_str())
                || inlet
                    .recipients
                    .iter()
                    .any(|r| recipients.contains(&r.as_str()))
            {
                return err("inboundのhandlerまたは宛先が重複しています".to_string());
            }
            let c = self.container.get(container).expect("resolve_handler 済み");
            if c.envs.contains_key("POCKET_INBOUND")
                || c.handlers
                    .values()
                    .any(|h| h.envs.contains_key("POCKET_INBOUND"))
            {
                return err("POCKET_INBOUND はpocketが管理します".to_string());
            }
            handlers.push(&inlet.handler);
            recipients.extend(inlet.recipients.iter().map(String::as_str));
        }
        Ok(())
    }

    fn check_scheduler_handlers(&self) -> Result<(), SettingsError> {
        let Some(scheduler) = self.scheduler.as_ref().filter(|s| !s.schedules.is_empty()) else {
            return Ok(());
        };
        let err = |m: String| Err(SettingsError::value(Vec::new(), m));
        if self.container.is_empty() {
            return err("scheduler is configured but container is missing".to_string());
        }
        const MANAGEMENT: &str = "pocket.django.lambda_handlers.management_command_handler";
        for (key, entry) in scheduler.schedules.iter() {
            let (_, _, handler) = match self.resolve_handler(&entry.handler) {
                Ok(r) => r,
                Err(m) => return err(format!("scheduler.schedules.{key}: {m}")),
            };
            match entry.scheduler {
                SchedulerKind::DjangoManagement if handler.command != MANAGEMENT => {
                    return err(format!(
                        "scheduler.schedules.{key}: scheduler='{}' requires the target handler \
                         '{}' to use command='{MANAGEMENT}', got '{}'",
                        misc::DJANGO_MANAGEMENT_SCHEDULER,
                        entry.handler,
                        handler.command
                    ));
                }
                SchedulerKind::Sqs if handler.sqs.is_none() => {
                    return err(format!(
                        "scheduler.schedules.{key}: scheduler='{}' requires the target handler \
                         '{}' to have sqs configured",
                        misc::SQS_SCHEDULER,
                        entry.handler
                    ));
                }
                _ => {}
            }
        }
        Ok(())
    }

    fn check_backup_targets(&self) -> Result<(), SettingsError> {
        let Some(backup) = &self.backup else {
            return Ok(());
        };
        let err = |m: &str| Err(SettingsError::value(Vec::new(), m));
        if backup.dsql.is_some() && self.dsql.is_none() {
            return err("[backup.dsql] には [dsql] の宣言が必要です。");
        }
        if backup.rds.is_some() {
            let Some(rds) = &self.rds else {
                return err("[backup.rds] には [rds] の宣言が必要です。");
            };
            if !rds.managed {
                return err(
                    "[backup.rds] は managed = true の [rds] でのみ使用できます \
                     (既存クラスタ参照 (managed = false) のバックアップは 参照元スタックの責務)。",
                );
            }
        }
        Ok(())
    }

    /// check_rds_vpc と check_rds_requires_container_vpc
    fn check_rds_vpc(&self) -> Result<(), SettingsError> {
        let Some(vpc) = self.rds.as_ref().and_then(|r| r.vpc.as_ref()) else {
            return Ok(());
        };
        if vpc.manage && vpc.zone_suffixes.len() < 2 {
            return Err(SettingsError::value(
                Vec::new(),
                "rds requires vpc with at least 2 zone_suffixes (DB Subnet Group needs 2+ AZs)",
            ));
        }
        if !self.container.values().any(|c| c.vpc.is_some()) {
            return Err(SettingsError::value(
                Vec::new(),
                "rds requires container with VPC",
            ));
        }
        Ok(())
    }

    fn check_rds_database_url_requires_vpc(&self) -> Result<(), SettingsError> {
        for (name, c) in self.container.iter() {
            let Some(secrets) = &c.secrets else { continue };
            if c.vpc.is_some() {
                continue;
            }
            for (key, spec) in secrets.managed.iter() {
                if spec.r#type == ManagedSecretType::RdsDatabaseUrl {
                    return Err(SettingsError::value(
                        Vec::new(),
                        format!(
                            "container.{name} は use_vpc=false のため [rds] に到達できず、\
                             secrets.managed.{key} (rds_database_url) は使えません。\
                             use_vpc=true にするか、この宣言を外してください"
                        ),
                    ));
                }
            }
        }
        Ok(())
    }

    /// key を宣言している (container 名, spec)。container 名順
    pub fn managed_secret_owners(&self, key: &str) -> Vec<(&str, &ManagedSecretSpec)> {
        let mut owners: Vec<(&str, &ManagedSecretSpec)> = self
            .container
            .iter()
            .filter_map(|(name, c)| Some((name.as_str(), c.secrets.as_ref()?.managed.get(key)?)))
            .collect();
        owners.sort_by(|a, b| a.0.cmp(b.0));
        owners
    }

    pub fn find_managed_secret(&self, key: &str) -> Option<&ManagedSecretSpec> {
        self.managed_secret_owners(key).first().map(|(_, s)| *s)
    }

    /// handler 参照 `<container>.<handler>` を解決する
    pub fn resolve_handler(&self, reference: &str) -> Result<(&str, &str, &LambdaHandler), String> {
        let Some((container, handler)) = reference.split_once('.') else {
            return Err(format!(
                "handler 参照 '{reference}' には container 名が必要です。 \
                 \"<container>.<handler>\" のドット記法で指定してください \
                 (例: handler = \"mydjango.wsgi\")。"
            ));
        };
        let Some((cname, c)) = self.container.iter().find(|(n, _)| *n == container) else {
            return Err(format!(
                "handler '{reference}': container '{container}' が [container.<name>] に\
                 ありません。"
            ));
        };
        let Some((hname, h)) = c.handlers.iter().find(|(n, _)| *n == handler) else {
            return Err(format!(
                "handler '{reference}': container '{container}' に handler '{handler}' がありません。"
            ));
        };
        Ok((cname.as_str(), hname.as_str(), h))
    }

    fn check_cloudfront_requires_s3(&self) -> Result<(), SettingsError> {
        if !self.cloudfront.is_empty() && self.s3.is_none() {
            return Err(SettingsError::value(
                Vec::new(),
                "s3 is required when cloudfront is configured",
            ));
        }
        for (name, cf) in self.cloudfront.iter() {
            self.check_cloudfront_entry(name, cf)
                .map_err(|m| SettingsError::value(Vec::new(), m))?;
        }
        Ok(())
    }

    fn check_cloudfront_entry(&self, name: &str, cf: &CloudFront) -> Result<(), String> {
        for route in &cf.routes {
            if route.signed && cf.signing_key.as_deref().unwrap_or("").is_empty() {
                return Err(format!(
                    "cloudfront.{name}: signing_key is required when route has signed=true"
                ));
            }
            if route.r#type == RouteType::Lambda {
                let reference = route.handler.as_deref().unwrap_or("");
                if reference.is_empty() {
                    return Err(format!(
                        "cloudfront.{name}: handler is required when route has type='lambda'"
                    ));
                }
                if self.container.is_empty() {
                    return Err(format!(
                        "cloudfront.{name}: container is required when route has type='lambda'"
                    ));
                }
                let (_, _, handler) = self
                    .resolve_handler(reference)
                    .map_err(|e| format!("cloudfront.{name}: {e}"))?;
                if handler.apigateway.is_none() {
                    return Err(format!(
                        "cloudfront.{name}: handler '{reference}' must have apigateway \
                         configured for lambda route"
                    ));
                }
            }
        }
        // _check_cloudfront_token_secret
        if let Some(token) = cf.token_secret.as_deref().filter(|t| !t.is_empty()) {
            if self.container.is_empty() {
                return Err(format!(
                    "cloudfront.{name}: container is required when token_secret is set"
                ));
            }
            if self.find_managed_secret(token).is_none() {
                return Err(format!(
                    "cloudfront.{name}: token_secret '{token}' not found in container \
                     secrets.managed"
                ));
            }
        }
        // _check_cloudfront_basic_auth
        if let Some(key) = cf.basic_auth.as_deref().filter(|k| !k.is_empty()) {
            let Some(spec) = self.find_managed_secret(key) else {
                return Err(format!(
                    "cloudfront.{name}: basic_auth '{key}' not found in container secrets.managed"
                ));
            };
            if spec.r#type != ManagedSecretType::BasicAuthCredential {
                return Err(format!(
                    "cloudfront.{name}: basic_auth '{key}' must be \
                     type='basic_auth_credential' (got '{}')",
                    managed_type_name(spec.r#type)
                ));
            }
        }
        // _check_cloudfront_secret_ref_uniqueness
        for (label, key) in [
            ("token_secret", &cf.token_secret),
            ("basic_auth", &cf.basic_auth),
            ("signing_key", &cf.signing_key),
        ] {
            let Some(key) = key.as_deref().filter(|k| !k.is_empty()) else {
                continue;
            };
            let owners = self.managed_secret_owners(key);
            let unshared: Vec<&str> = owners
                .iter()
                .filter(|(_, s)| !s.shared)
                .map(|(n, _)| *n)
                .collect();
            let has_shared = owners.iter().any(|(_, s)| s.shared);
            if unshared.len() + usize::from(has_shared) > 1 {
                let note = if has_shared { "あり" } else { "なし" };
                return Err(format!(
                    "cloudfront.{name}: {label} '{key}' の値の候補が複数ありどれを使うか曖昧です \
                     (shared 宣言: {note} / 無印宣言: {})。参照される secret は全宣言に \
                     shared = true を付けて値を 1 つにするか、一意の key 名にしてください。",
                    unshared.join(", ")
                ));
            }
        }
        // _check_cloudfront_origin_verify
        if cf.enable_origin_verify && !cf.routes.iter().any(|r| r.r#type == RouteType::Lambda) {
            return Err(format!(
                "cloudfront.{name}: enable_origin_verify requires at least one lambda route \
                 (the origin to protect). S3-only distributions are already protected by OAC."
            ));
        }
        // _check_cloudfront_waf_allow_rules
        if let Some(waf) = &cf.waf {
            let mut headers: Vec<&str> = waf
                .allow_rules
                .iter()
                .filter_map(|r| r.header.as_deref().filter(|h| !h.is_empty()))
                .collect();
            if !headers.is_empty() {
                if self.container.is_empty() {
                    return Err(format!(
                        "cloudfront.{name}: container is required when waf.allow_rules has header"
                    ));
                }
                headers.sort();
                headers.dedup();
                for header in headers {
                    let declared = self.container.values().any(|c| {
                        c.secrets.as_ref().is_some_and(|s| {
                            s.managed.contains_key(header) || s.user.contains_key(header)
                        })
                    });
                    if declared {
                        return Err(format!(
                            "cloudfront.{name}: waf.allow_rules header '{header}' は container の \
                             secrets に既に宣言されています。secret は pocket が自動生成するため、\
                             別のキー名を使ってください。"
                        ));
                    }
                }
            }
        }
        Ok(())
    }

    fn check_container_names(&self) -> Result<(), SettingsError> {
        static RE: OnceLock<Regex> = OnceLock::new();
        let re = RE.get_or_init(|| Regex::new("^[a-z][a-z0-9]{0,31}$").expect("re"));
        for name in self.container.keys() {
            if !re.is_match(name) {
                return Err(SettingsError::value(
                    Vec::new(),
                    format!(
                        "container 名 '{name}' が不正です。英小文字始まりの英小文字+数字 \
                         (最大 32 文字、hyphen 不可) にしてください。物理リソース名の \
                         slot とドット記法の一意分解に使うための制約です。"
                    ),
                ));
            }
        }
        Ok(())
    }

    /// container 間の secrets 宣言の整合性 (store / pocket_key_format の一致、
    /// shared managed secret と user secret の同名宣言の spec 一致)
    fn check_container_secrets_consistency(&self) -> Result<(), SettingsError> {
        let err = |m: String| Err(SettingsError::value(Vec::new(), m));
        let mut stores: Vec<(&str, StoreType)> = Vec::new();
        let mut formats: Vec<(&str, &str)> = Vec::new();
        let mut shared: Vec<(&str, &str, &ManagedSecretSpec)> = Vec::new();
        let mut users: Vec<(&str, &str, &UserSecretSpec)> = Vec::new();
        for (name, c) in self.container.iter() {
            let Some(secrets) = &c.secrets else { continue };
            stores.push((name, secrets.store));
            formats.push((name, secrets.pocket_key_format.as_str()));
            for (key, spec) in secrets.managed.iter() {
                if !spec.shared {
                    continue;
                }
                match shared.iter().find(|(k, _, _)| k == key) {
                    Some((_, owner, first)) if *first != spec => {
                        return err(format!(
                            "shared managed secret '{key}' が container '{owner}' と '{name}' で\
                             異なる spec で宣言されています。共有する secret は\
                             全宣言で同一 spec にしてください。"
                        ))
                    }
                    Some(_) => {}
                    None => shared.push((key, name, spec)),
                }
            }
            for (key, spec) in secrets.user.iter() {
                match users.iter().find(|(k, _, _)| k == key) {
                    Some((_, owner, first)) if *first != spec => {
                        return err(format!(
                            "user secret '{key}' が container '{owner}' と '{name}' で異なる \
                             spec で宣言されています。同名 key は同一 spec にするか、\
                             別の key 名を使ってください。"
                        ))
                    }
                    Some(_) => {}
                    None => users.push((key, name, spec)),
                }
            }
        }
        let listed = |pairs: Vec<(&str, String)>| {
            let mut pairs = pairs;
            pairs.sort();
            pairs
                .iter()
                .map(|(n, v)| format!("{n}={v}"))
                .collect::<Vec<_>>()
                .join(", ")
        };
        let distinct_stores: std::collections::BTreeSet<_> =
            stores.iter().map(|(_, s)| *s as u8).collect();
        if distinct_stores.len() > 1 {
            let pairs = stores
                .iter()
                .map(|(n, s)| (*n, store_name(*s).to_string()))
                .collect();
            return err(format!(
                "secrets.store は全 container で一致が必要です: {}",
                listed(pairs)
            ));
        }
        let distinct_formats: std::collections::BTreeSet<_> =
            formats.iter().map(|(_, f)| *f).collect();
        if distinct_formats.len() > 1 {
            let pairs = formats.iter().map(|(n, f)| (*n, f.to_string())).collect();
            return err(format!(
                "secrets.pocket_key_format は全 container で一致が必要です: {}",
                listed(pairs)
            ));
        }
        Ok(())
    }
}

fn store_name(s: StoreType) -> &'static str {
    match s {
        StoreType::Sm => "sm",
        StoreType::Ssm => "ssm",
    }
}

fn managed_type_name(t: ManagedSecretType) -> String {
    serde_json::to_value(t)
        .ok()
        .and_then(|v| v.as_str().map(str::to_string))
        .unwrap_or_default()
}
