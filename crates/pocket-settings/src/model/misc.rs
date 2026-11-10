//! `[ses]` / `[inbound.<name>]` / `[scheduler]` (Python の `Ses` / `Inbound` / `Scheduler` と
//! schedule entry 3 種)。

use std::sync::OnceLock;

use regex::Regex;
use serde::ser::SerializeMap;
use serde::{Deserialize, Serialize};

use crate::error::{child, PathSeg, SettingsError};
use crate::model::container::Alert;
use crate::model::general::default_true;
use crate::types::{AnyDict, OrderedMap};

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Ses {
    pub from_email: String,
    /// None なら `[general] region` を使う
    #[serde(default)]
    pub region: Option<String>,
    #[serde(default)]
    pub configuration_set: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize, Serialize)]
pub enum TlsPolicy {
    #[default]
    Require,
    Optional,
}

/// SES 受信口
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Inbound {
    pub domain: String,
    #[serde(default = "default_true")]
    pub manage_dns: bool,
    #[serde(default)]
    pub hosted_zone_id_override: Option<String>,
    #[serde(deserialize_with = "non_empty_list")]
    pub recipients: Vec<String>,
    pub handler: String,
    #[serde(default)]
    pub rule_set: Option<String>,
    #[serde(default = "default_true")]
    pub enabled: bool,
    #[serde(default)]
    pub tls_policy: TlsPolicy,
    #[serde(default = "default_true")]
    pub scan_enabled: bool,
    #[serde(default = "default_raw_prefix")]
    pub raw_prefix: String,
    #[serde(default = "default_metadata_prefix")]
    pub metadata_prefix: String,
    #[serde(default = "default_import_prefix")]
    pub import_prefix: String,
    #[serde(deserialize_with = "crate::types::ge14")]
    pub retention_days: i64,
    pub delivery_alert: Alert,
}

fn default_raw_prefix() -> String {
    "raw/".to_string()
}
fn default_metadata_prefix() -> String {
    "metadata/".to_string()
}
fn default_import_prefix() -> String {
    "imports/".to_string()
}

/// Python の `Field(min_length=1)` の list
fn non_empty_list<'de, D: serde::Deserializer<'de>>(d: D) -> Result<Vec<String>, D::Error> {
    use serde::de::Error as _;
    let v = Vec::<String>::deserialize(d)?;
    if v.is_empty() {
        return Err(D::Error::custom(
            "List should have at least 1 item after validation, not 0",
        ));
    }
    Ok(v)
}

impl Inbound {
    pub(crate) fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        self.delivery_alert
            .validate(&child(path, "delivery_alert"))?;
        let err = |m: &str| Err(SettingsError::value(path.to_vec(), m));
        // validate_inbound
        static DOMAIN: OnceLock<Regex> = OnceLock::new();
        let domain = DOMAIN.get_or_init(|| {
            Regex::new(
                r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+)$",
            )
            .expect("re")
        });
        if !domain.is_match(&self.domain) {
            return err("inbound.domain は小文字のDNS名で指定してください");
        }
        let mut unique = self.recipients.clone();
        unique.sort();
        unique.dedup();
        if unique.len() != self.recipients.len() {
            return err("受信宛先が重複しています");
        }
        let recipient = Regex::new(&format!(
            "^(?:[a-z0-9!#$%&'+/=?^_`{{|}}~.-]+@{})$",
            regex::escape(&self.domain)
        ))
        .expect("re");
        if !self.recipients.iter().all(|a| recipient.is_match(a)) {
            return err("recipients はdomain内の完全なメールアドレスを指定してください");
        }
        static PREFIX: OnceLock<Regex> = OnceLock::new();
        let prefix_re =
            PREFIX.get_or_init(|| Regex::new("^(?:[a-zA-Z0-9][a-zA-Z0-9/_-]*/)$").expect("re"));
        let prefixes = [
            self.raw_prefix.as_str(),
            self.metadata_prefix.as_str(),
            self.import_prefix.as_str(),
            "attachments/",
        ];
        if !prefixes.iter().all(|p| prefix_re.is_match(p)) {
            return err("prefixは安全な文字で指定し末尾を / にしてください");
        }
        for (i, p) in prefixes.iter().enumerate() {
            if prefixes[i + 1..]
                .iter()
                .any(|o| p.starts_with(o) || o.starts_with(p))
            {
                return err("inboundのprefixが重複しています");
            }
        }
        Ok(())
    }
}

pub(crate) const LAMBDA_SCHEDULER: &str = "pocket.lambda_scheduler";
pub(crate) const DJANGO_MANAGEMENT_SCHEDULER: &str = "pocket.django.management_lambda_scheduler";
pub(crate) const SQS_SCHEDULER: &str = "pocket.sqs_scheduler";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
pub enum SchedulerKind {
    #[serde(rename = "pocket.lambda_scheduler")]
    Lambda,
    #[serde(rename = "pocket.django.management_lambda_scheduler")]
    DjangoManagement,
    #[serde(rename = "pocket.sqs_scheduler")]
    Sqs,
}

/// schedule entry。Python では `scheduler` で 3 種のモデルに分かれる (discriminated union)。
/// 種類ごとのモデルに分けて読むと entry 内のエラーの path が途切れるため、全種類の
/// field を持つ 1 つの struct で読み、種類に合わない field は検証で拒否する
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScheduleEntry {
    #[serde(default)]
    pub cron: Option<String>,
    #[serde(default)]
    pub rate: Option<String>,
    pub handler: String,
    /// 省略時は前処理で `pocket.lambda_scheduler` を補う
    pub scheduler: SchedulerKind,
    /// lambda: Lambda に渡す input
    #[serde(default)]
    pub input: Option<AnyDict>,
    /// django management: 実行する command line
    #[serde(default)]
    pub manage: Option<String>,
    /// sqs: queue に送る MessageBody
    #[serde(default)]
    pub message: Option<AnyDict>,
}

impl ScheduleEntry {
    /// EventBridge の schedule 式 (`cron(...)` / `rate(...)`)
    pub fn schedule_expression(&self) -> String {
        match self.cron.as_deref().filter(|c| !c.is_empty()) {
            Some(cron) => format!("cron({cron})"),
            None => format!("rate({})", self.rate.as_deref().unwrap_or("")),
        }
    }

    fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        // 種類に合わない field (Python では extra="forbid") と、必須の field
        let extra = |name: &str| SettingsError {
            path: child(path, name),
            kind: crate::ErrorKind::Extra,
            message: "Extra inputs are not permitted".to_string(),
        };
        match self.scheduler {
            SchedulerKind::Lambda => {
                if self.manage.is_some() {
                    return Err(extra("manage"));
                }
                if self.message.is_some() {
                    return Err(extra("message"));
                }
            }
            SchedulerKind::DjangoManagement => {
                if self.input.is_some() {
                    return Err(extra("input"));
                }
                if self.message.is_some() {
                    return Err(extra("message"));
                }
                if self.manage.is_none() {
                    return Err(SettingsError {
                        path: child(path, "manage"),
                        kind: crate::ErrorKind::Missing,
                        message: "Field required".to_string(),
                    });
                }
            }
            SchedulerKind::Sqs => {
                if self.input.is_some() {
                    return Err(extra("input"));
                }
                if self.manage.is_some() {
                    return Err(extra("manage"));
                }
            }
        }
        let err = |m: &str| Err(SettingsError::value(path.to_vec(), m));
        // check_cron_or_rate
        let has_cron = self.cron.as_deref().is_some_and(|c| !c.is_empty());
        let has_rate = self.rate.as_deref().is_some_and(|r| !r.is_empty());
        if has_cron == has_rate {
            return err("schedule entry must specify exactly one of cron / rate");
        }
        // check_manage_not_empty
        if self.scheduler == SchedulerKind::DjangoManagement
            && self.manage.as_deref().unwrap_or("").trim().is_empty()
        {
            return err("manage must be a non-empty shell-style command");
        }
        Ok(())
    }
}

/// Python の各 entry モデルの `model_dump(mode="json")` と同じ形
impl Serialize for ScheduleEntry {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        let empty = || serde_json::Value::Object(serde_json::Map::new());
        let mut m = s.serialize_map(None)?;
        m.serialize_entry("cron", &self.cron)?;
        m.serialize_entry("rate", &self.rate)?;
        m.serialize_entry("handler", &self.handler)?;
        m.serialize_entry("scheduler", &self.scheduler)?;
        match self.scheduler {
            SchedulerKind::Lambda => {
                let input = self
                    .input
                    .as_ref()
                    .map(|d| d.0.clone())
                    .unwrap_or_else(empty);
                m.serialize_entry("input", &input)?;
            }
            SchedulerKind::DjangoManagement => m.serialize_entry("manage", &self.manage)?,
            SchedulerKind::Sqs => {
                let message = self
                    .message
                    .as_ref()
                    .map(|d| d.0.clone())
                    .unwrap_or_else(empty);
                m.serialize_entry("message", &message)?;
            }
        }
        m.serialize_entry("schedule_expression", &self.schedule_expression())?;
        m.end()
    }
}

#[derive(Debug, Clone, PartialEq, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Scheduler {
    #[serde(default)]
    pub schedules: OrderedMap<ScheduleEntry>,
}

impl Scheduler {
    pub(crate) fn validate(&self, path: &[PathSeg]) -> Result<(), SettingsError> {
        let schedules = child(path, "schedules");
        for (key, entry) in self.schedules.iter() {
            entry.validate(&child(&schedules, key))?;
        }
        Ok(())
    }
}

/// schedule entry の `scheduler` 省略時に既定を補う (Python の `normalize_entries`)
pub(crate) fn fill_default_scheduler(data: &mut toml::Table) {
    let Some(toml::Value::Table(scheduler)) = data.get_mut("scheduler") else {
        return;
    };
    let Some(toml::Value::Table(schedules)) = scheduler.get_mut("schedules") else {
        return;
    };
    for (_, entry) in schedules.iter_mut() {
        if let toml::Value::Table(e) = entry {
            e.entry("scheduler")
                .or_insert_with(|| toml::Value::String(LAMBDA_SCHEDULER.to_string()));
        }
    }
}
