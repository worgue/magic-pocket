//! `pocket.context` (Settings から deploy 用の値を導出した Context) の移植。
//!
//! Python の `Context.from_settings` (`pocket/context.py`) と同じ順で組み立て、
//! `model_dump(mode="json")` と同じ形 (computed field を含み、同じ key 順) の JSON を
//! 出す。一致は共有 test vector (`tests/data/context_vectors`) で固定している。
//!
//! Python の Context は computed field を dump 時に遅延評価するが、ここでは構築時に
//! 値を確定して struct の field に持つ (field の宣言順 = JSON の key 順)。
//!
//! AWS / git / ファイルシステムは [`ExternalInputs`] で注入する (このクレートは
//! AWS SDK に依存しない)。環境変数は [`ContextInputs::env`] で渡す。

mod build;
mod cloudfront;
mod container;
mod general;
mod services;

use std::collections::BTreeMap;

use serde::Serialize;

pub use cloudfront::{
    CloudFrontContext, CloudFrontWafAllowRuleContext, CloudFrontWafContext,
    CloudFrontWafRateLimitContext, RedirectFromContext, RouteContext,
};
pub use container::{
    ApiGatewayContext, BuildContext, ContainerContext, ContainerIamContext, LambdaHandlerContext,
    SecretsContext, SqsContext, SqsDeadLetterAlertContext,
};
pub use general::{
    DjangoCacheContext, DjangoContext, DjangoStorageContext, EfsContext, GeneralContext,
    InboundContext, InboundDomainContext, VpcContext,
};
pub use services::{
    BackupContext, BackupPlanContext, BackupRuleContext, DsqlContext, NeonContext, RdsContext,
    S3Context, S3CorsContext, S3LifecycleRuleContext, ScheduleEntryContext, SchedulerContext,
    SesContext, TiDbContext, UpstashContext,
};

use crate::types::OrderedMap;
use crate::Settings;

/// Route53 の hosted zone (`list_hosted_zones` の 1 件)
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HostedZone {
    /// 末尾 `.` 付きの zone 名 (API の値のまま)
    pub name: String,
    /// `/hostedzone/<id>` (API の値のまま)
    pub id: String,
}

/// Context が読む外部の状態。CLI は AWS / git / ファイルシステムで、test は
/// vector に同梱した固定値で実装する。
///
/// Python で該当する箇所:
/// - `hosted_zones`: `pocket.utils.get_hosted_zones` (Route53 `list_hosted_zones`)
/// - `managed_secret_arn`: `SecretsManager.arn` (`get_secret_value` の ARN)
/// - `git_short_hash`: `_get_deploy_hash` の `git rev-parse --short HEAD`
/// - `dir_files`: `CloudFrontContext.managed_asset_files` の `Path.iterdir`
pub trait ExternalInputs {
    /// Route53 の hosted zone の一覧。Context の構築中に高々 1 回呼ぶ
    fn hosted_zones(&self) -> Result<Vec<HostedZone>, String>;

    /// SM に保存した managed secret (SecretId = `pocket_key`) の ARN。
    /// 無ければ `Ok(None)` (Python の `PocketSecretIsNotReady`)
    fn managed_secret_arn(&self, pocket_key: &str, region: &str) -> Result<Option<String>, String>;

    /// `git rev-parse --short HEAD` の出力 (前後の空白を除く)。失敗なら `None`
    fn git_short_hash(&self) -> Option<String>;

    /// dir 直下のファイル名 (列挙順は OS のまま)。dir でなければ `None`
    fn dir_files(&self, dir: &str) -> Option<Vec<String>>;
}

/// Context の入力のうち Settings 以外
pub struct ContextInputs<'a> {
    /// 環境変数 (`POCKET_PERMISSIONS_BOUNDARY_ARN` / `FORGE_PERMISSIONS_BOUNDARY_ARN` /
    /// `DEPLOY_HASH` / `AWS_LAMBDA_FUNCTION_NAME`)
    pub env: &'a BTreeMap<String, String>,
    pub external: &'a dyn ExternalInputs,
}

/// Context の構築の失敗 (Python では ValueError / RuntimeError / Exception)
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct ContextError(pub String);

impl ContextError {
    pub(crate) fn new(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}

/// 構築中に Python 版が stderr に出す log / warning (`echo.log` / `echo.warning`)
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContextMessage {
    pub level: MessageLevel,
    pub message: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MessageLevel {
    Log,
    Warning,
}

#[derive(Debug, Clone, Serialize)]
pub struct Context {
    pub general: Option<GeneralContext>,
    pub container: OrderedMap<ContainerContext>,
    /// shared store (project 共有パス) の union view
    pub secrets: Option<SecretsContext>,
    pub neon: Option<NeonContext>,
    pub tidb: Option<TiDbContext>,
    pub upstash: Option<UpstashContext>,
    pub dsql: Option<DsqlContext>,
    pub rds: Option<RdsContext>,
    pub backup: Option<BackupContext>,
    pub ses: Option<SesContext>,
    pub inbound: OrderedMap<InboundContext>,
    pub inbound_domain: OrderedMap<InboundDomainContext>,
    pub s3: Option<S3Context>,
    pub cloudfront: OrderedMap<CloudFrontContext>,
    pub scheduler: OrderedMap<SchedulerContext>,
    pub external_roles: bool,
    pub external_role_prefix: String,
    pub project_name: String,
    pub stage: String,
}

impl Context {
    /// Python の `Context.from_settings`。構築中の log / warning も返す
    pub fn from_settings(
        settings: &Settings,
        inputs: &ContextInputs,
    ) -> Result<(Context, Vec<ContextMessage>), ContextError> {
        let builder = build::Builder::new(settings, inputs);
        let context = builder.context()?;
        Ok((context, builder.into_messages()))
    }

    /// Python の `model_dump(mode="json")` と同じ形の JSON
    pub fn to_json(&self) -> serde_json::Value {
        serde_json::to_value(self).expect("Context は JSON にできる")
    }
}

/// Python の `str.capitalize()` (先頭を大文字、残りを小文字)
pub(crate) fn capitalize(s: &str) -> String {
    let mut chars = s.chars();
    match chars.next() {
        Some(first) => first
            .to_uppercase()
            .chain(chars.flat_map(char::to_lowercase))
            .collect(),
        None => String::new(),
    }
}

/// `pocket.naming.user_secret_path`
pub(crate) fn user_secret_path(
    pocket_key: &str,
    segment: &str,
    store: crate::model::StoreType,
) -> String {
    let prefix = format!("{pocket_key}-user");
    match store {
        crate::model::StoreType::Ssm => format!("/{prefix}/{segment}"),
        crate::model::StoreType::Sm => format!("{prefix}/{segment}"),
    }
}

/// `{namespace}` / `{stage}` / `{project}` 等を展開する (Python の `str.format(**vars)`)
pub(crate) fn pyformat(template: &str, vars: &[(&str, &str)]) -> String {
    let mut out = template.to_string();
    for (k, v) in vars {
        out = out.replace(&format!("{{{k}}}"), v);
    }
    out
}

/// sha256 の hex の先頭 `n` 文字
pub(crate) fn sha256_hex_prefix(data: &str, n: usize) -> String {
    use sha2::{Digest, Sha256};
    let digest = Sha256::digest(data.as_bytes());
    let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
    hex[..n].to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_capitalize_matches_python() {
        assert_eq!(capitalize("wsgi"), "Wsgi");
        assert_eq!(capitalize("aPI"), "Api");
        assert_eq!(capitalize(""), "");
    }

    #[test]
    fn test_sha256_hex_prefix() {
        // hashlib.sha256(b"abc").hexdigest()[:10]
        assert_eq!(sha256_hex_prefix("abc", 10), "ba7816bf8f");
    }
}
