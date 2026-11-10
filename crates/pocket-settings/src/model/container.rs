//! `[container.<name>]` 一式 (Python の `Container` / `LambdaHandler` / `Secrets` 等) と、
//! handler が使う `Sqs` / `Alert` / `ApiGateway`。

use std::collections::BTreeMap;
use std::sync::OnceLock;

use regex::Regex;
use serde::{Deserialize, Serialize};

use crate::error::{child, PathSeg, SettingsError};
use crate::model::django::Django;
use crate::model::general::{default_true, Vpc};
use crate::types::{AnyDict, FormatStr, OrderedMap, StrOrInt};

/// 名前の制約 (Python の `EnvStr`)。dict の key に使うので型ではなく検証関数で見る
const ENV_STR_PATTERN: &str = "^[a-zA-Z0-9_]+$";

fn env_str_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(ENV_STR_PATTERN).expect("pattern"))
}

/// dict の key の検証エラー。pydantic と同じく path は `<key>` の後ろに `[key]`
fn check_env_str_keys<V>(map: &OrderedMap<V>, path: &[PathSeg]) -> Result<(), SettingsError> {
    for key in map.keys() {
        if !env_str_re().is_match(key) {
            let mut p = child(path, key);
            p.push(PathSeg::Key("[key]".to_string()));
            return Err(SettingsError {
                path: p,
                kind: crate::ErrorKind::Type,
                message: format!("String should match pattern '{ENV_STR_PATTERN}'"),
            });
        }
    }
    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum StoreType {
    #[default]
    Sm,
    Ssm,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ManagedSecretType {
    AutoDatabaseUrl,
    Password,
    NeonDatabaseUrl,
    TidbDatabaseUrl,
    RdsDatabaseUrl,
    UpstashRedisUrl,
    RsaPemBase64,
    CloudfrontSigningKey,
    SpaTokenSecret,
    OriginVerifySecret,
    WafAllowSecret,
    BasicAuthCredential,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ManagedSecretSpec {
    pub r#type: ManagedSecretType,
    #[serde(default)]
    pub options: OrderedMap<StrOrInt>,
    /// true なら project 共有の store に保存し、同名 + 同 spec の container で共有する
    #[serde(default)]
    pub shared: bool,
}

impl ManagedSecretSpec {
    fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        // check_basic_auth_options
        if self.r#type != ManagedSecretType::BasicAuthCredential {
            return Ok(());
        }
        let err = |m: &str| Err(SettingsError::value(path.to_vec(), m));
        let username = match self.options.get("username") {
            Some(StrOrInt::Str(s)) if !s.is_empty() => s,
            _ => return err("basic_auth_credential requires options.username (str)"),
        };
        if username.contains(':') {
            return err("basic_auth_credential username must not contain ':'");
        }
        match self.options.get("password") {
            None => {}
            Some(StrOrInt::Str(s)) if !s.is_empty() => {}
            Some(_) => {
                return err("basic_auth_credential options.password must be a non-empty string")
            }
        }
        Ok(())
    }
}

/// user secret の stored mode 用 type
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum UserSecretType {
    NeonDatabaseUrl,
    TidbDatabaseUrl,
    UpstashRedisUrl,
}

impl UserSecretType {
    fn as_str(&self) -> &'static str {
        match self {
            Self::NeonDatabaseUrl => "neon_database_url",
            Self::TidbDatabaseUrl => "tidb_database_url",
            Self::UpstashRedisUrl => "upstash_redis_url",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct UserSecretSpec {
    /// SM: ARN か secret 名、SSM: parameter 名 / path
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub r#type: Option<UserSecretType>,
    /// None なら `Secrets.store` を継承する
    #[serde(default)]
    pub store: Option<StoreType>,
}

impl UserSecretSpec {
    fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        // check_name_or_type
        if self.name.is_none() && self.r#type.is_none() {
            return Err(SettingsError::value(
                path.to_vec(),
                "UserSecretSpec requires 'name' or 'type' \
                 (name = 明示参照 / type = stored mode で正準名を自動導出).",
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Secrets {
    #[serde(default)]
    pub store: StoreType,
    #[serde(default = "default_pocket_key_format")]
    pub pocket_key_format: FormatStr,
    #[serde(default)]
    pub managed: OrderedMap<ManagedSecretSpec>,
    #[serde(default)]
    pub user: OrderedMap<UserSecretSpec>,
    #[serde(default)]
    pub extra_resources: Vec<String>,
    #[serde(default)]
    pub require_list_secrets: bool,
}

fn default_pocket_key_format() -> FormatStr {
    FormatStr("{stage}-{project}-{namespace}".to_string())
}

impl Secrets {
    fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        let managed = child(path, "managed");
        check_env_str_keys(&self.managed, &managed)?;
        for (key, spec) in self.managed.iter() {
            spec.validate(&child(&managed, key))?;
        }
        let user = child(path, "user");
        check_env_str_keys(&self.user, &user)?;
        for (key, spec) in self.user.iter() {
            spec.validate(&child(&user, key))?;
        }
        let err = |m: String| Err(SettingsError::value(path.to_vec(), m));
        // check_user_name_type_exclusive
        for (key, spec) in self.user.iter() {
            if spec.name.is_some() && spec.r#type.is_some() {
                return err(format!(
                    "user secret '{key}': 'name' と 'type' は排他です \
                     (name = 明示参照 / type = stored mode のどちらか一方のみ)."
                ));
            }
        }
        // check_user_type_unique
        let mut seen: Vec<(UserSecretType, &String)> = Vec::new();
        for (key, spec) in self.user.iter() {
            let Some(t) = spec.r#type else { continue };
            if let Some((_, first)) = seen.iter().find(|(s, _)| *s == t) {
                return err(format!(
                    "user secret '{key}' と '{first}' が同一 type={} です。stored の保存パスは \
                     type 基準で導出されるため、1 stage につき同一 type は 1 個までに \
                     してください。",
                    t.as_str()
                ));
            }
            seen.push((t, key));
        }
        Ok(())
    }
}

/// Lambda execution role に追加する IAM 設定
#[derive(Debug, Clone, PartialEq, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ContainerIam {
    #[serde(default)]
    pub managed_policy_arns: Vec<String>,
    /// key は PolicyName の suffix、value は PolicyDocument
    #[serde(default)]
    pub inline_policies: OrderedMap<AnyDict>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum BuildBackend {
    #[default]
    Codebuild,
    Docker,
    Depot,
}

/// `build = "docker"` の文字列形は前処理で `{ backend = "docker" }` にしてから読む
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct BuildConfig {
    #[serde(default)]
    pub backend: BuildBackend,
    #[serde(default = "default_compute_type")]
    pub compute_type: String,
    #[serde(default)]
    pub depot_project_id: Option<String>,
}

impl Default for BuildConfig {
    fn default() -> Self {
        Self {
            backend: BuildBackend::Codebuild,
            compute_type: default_compute_type(),
            depot_project_id: None,
        }
    }
}

fn default_compute_type() -> String {
    "BUILD_GENERAL1_MEDIUM".to_string()
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize, Serialize)]
pub enum Platform {
    #[default]
    #[serde(rename = "linux/amd64")]
    LinuxAmd64,
    #[serde(rename = "linux/arm64")]
    LinuxArm64,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ApiGateway {
    #[serde(default)]
    pub domain: Option<String>,
    #[serde(default = "default_true")]
    pub create_records: bool,
    #[serde(default)]
    pub hosted_zone_id_override: Option<String>,
}

/// 通知宣言の共通形 (`Sqs.dead_letter_alert` / `Inbound.delivery_alert`)
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Alert {
    #[serde(default = "default_true")]
    pub enabled: bool,
    #[serde(default)]
    pub email: Option<String>,
    #[serde(default)]
    pub eventbridge: bool,
}

impl Alert {
    pub(crate) fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        // check_email_required_when_enabled
        if !self.enabled {
            return Ok(());
        }
        let has_email = self.email.as_deref().is_some_and(|e| !e.is_empty());
        if has_email == self.eventbridge {
            return Err(SettingsError::value(
                path.to_vec(),
                "alert requires email or eventbridge = true (どちらか一方)",
            ));
        }
        static RE: OnceLock<Regex> = OnceLock::new();
        let re = RE.get_or_init(|| Regex::new(r"^(?:[^\s@]+@[^\s@]+\.[^\s@]+)$").expect("re"));
        if let Some(email) = self.email.as_deref().filter(|e| !e.is_empty()) {
            if !re.is_match(email) {
                return Err(SettingsError::value(
                    path.to_vec(),
                    "通知先emailの形式が不正です",
                ));
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Sqs {
    #[serde(default = "default_batch_size")]
    pub batch_size: i64,
    #[serde(default = "default_message_retention_period")]
    pub message_retention_period: i64,
    #[serde(default = "default_maximum_concurrency")]
    pub maximum_concurrency: i64,
    #[serde(default = "default_dead_letter_max_receive_count")]
    pub dead_letter_max_receive_count: i64,
    #[serde(default = "default_dead_letter_message_retention_period")]
    pub dead_letter_message_retention_period: i64,
    #[serde(default = "default_true")]
    pub report_batch_item_failures: bool,
    /// DLQ 到達の通知宣言 (必須。無監視にするなら `enabled = false` を明示する)
    #[serde(default)]
    pub dead_letter_alert: Option<Alert>,
}

fn default_batch_size() -> i64 {
    10
}
fn default_message_retention_period() -> i64 {
    345600
}
fn default_maximum_concurrency() -> i64 {
    2
}
fn default_dead_letter_max_receive_count() -> i64 {
    5
}
fn default_dead_letter_message_retention_period() -> i64 {
    1209600
}

impl Sqs {
    fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        if let Some(alert) = &self.dead_letter_alert {
            alert.validate(&child(path, "dead_letter_alert"))?;
        }
        // check_dead_letter_alert_declared
        if self.dead_letter_alert.is_none() {
            return Err(SettingsError::value(
                path.to_vec(),
                "sqs.dead_letter_alert is required so that messages falling into the \
                 dead-letter queue never go unnoticed. Either declare a notification \
                 target:\n  sqs = { dead_letter_alert = { email = \"ops@example.com\" } }\n\
                 or explicitly opt out of monitoring:\n  \
                 sqs = { dead_letter_alert = { enabled = false } }",
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct LambdaHandler {
    pub command: String,
    #[serde(default = "default_timeout")]
    pub timeout: i64,
    #[serde(default = "default_memory_size")]
    pub memory_size: i64,
    #[serde(default)]
    pub reserved_concurrency: Option<i64>,
    #[serde(default)]
    pub apigateway: Option<ApiGateway>,
    #[serde(default)]
    pub sqs: Option<Sqs>,
    /// handler 単位の env の追加・上書き (container の envs より優先)
    #[serde(default)]
    pub envs: BTreeMap<String, String>,
}

fn default_timeout() -> i64 {
    30
}
fn default_memory_size() -> i64 {
    512
}

const MANAGEMENT_COMMAND_HANDLER: &str = "pocket.django.lambda_handlers.management_command_handler";

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Container {
    #[serde(default)]
    pub vpc: Option<Vpc>,
    #[serde(default)]
    pub secrets: Option<Secrets>,
    #[serde(default)]
    pub handlers: OrderedMap<LambdaHandler>,
    pub dockerfile_path: String,
    #[serde(default)]
    pub envs: BTreeMap<String, String>,
    /// Lambda の Architectures もこの値から導出する
    #[serde(default)]
    pub platform: Platform,
    #[serde(default)]
    pub django: Option<Django>,
    #[serde(default)]
    pub permissions_boundary: Option<String>,
    #[serde(default)]
    pub iam: ContainerIam,
    #[serde(default)]
    pub build: BuildConfig,
    /// ECR repository 名の上書き
    #[serde(default)]
    pub ecr_name: Option<String>,
}

impl Container {
    pub(crate) fn validate(&mut self, path: &[PathSeg]) -> Result<(), SettingsError> {
        if let Some(vpc) = &self.vpc {
            vpc.validate(&child(path, "vpc"))?;
        }
        if let Some(secrets) = &self.secrets {
            secrets.validate(&child(path, "secrets"))?;
        }
        let handlers = child(path, "handlers");
        for (name, h) in self.handlers.iter() {
            if let Some(sqs) = &h.sqs {
                sqs.validate(&child(&child(&handlers, name), "sqs"))?;
            }
        }
        if let Some(django) = &mut self.django {
            django.validate(&child(path, "django"))?;
        }
        // check_handlers
        let management = self
            .handlers
            .values()
            .filter(|h| h.command == MANAGEMENT_COMMAND_HANDLER)
            .count();
        if management > 1 {
            return Err(SettingsError::value(
                path.to_vec(),
                "Only one management command handler is allowed.",
            ));
        }
        Ok(())
    }
}
