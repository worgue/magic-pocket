//! SES 受信 (inbound) の worker ヘルパ。Python 側 `pocket.inbound` と同じ契約。
//!
//! pocket の `[inbound.<name>]` は SES の S3Action で MIME 原本を非公開 bucket に
//! 保存し、その SNS 通知を handler の SQS queue へ配送する。runtime には
//! `POCKET_INBOUND` (受信口ごとの bucket / prefix / topic / 宛先の JSON) が注入される。
//! 本モジュールはその上で次を提供する:
//!
//! - [`Receiver::receive`]: SNS envelope と SES 通知を検証し、原本の VersionId と
//!   SHA256、完全な通知を `metadata/<受信ID>.json` に**条件付き保存**してから
//!   [`ReceivedMail`] を返す。同じ通知の再送・同時受信では保存済みの版に揃える
//! - [`process_inbound_records`]: SqsEvent を record 単位で受信 → 利用側処理へ渡し、
//!   失敗 record だけを partial batch response で報告する ([`crate::sqs`] と同じ規約)
//! - [`Receiver::load_import`]: `pocket resource inbound copy` で揃えた
//!   原本 + 受信情報を明示的な取り込みジョブから読む
//!
//! MIME の解析は利用側の責務 (Python 側が標準ライブラリの `email` を使うのに対し
//! Rust では `mail-parser` 等を利用側で選ぶ)。本文・添付・HTML は信頼できない入力
//! として扱うこと。
//!
//! # Example (worker バイナリ)
//!
//! ```ignore
//! use aws_lambda_events::event::sqs::{SqsBatchResponse, SqsEvent};
//! use lambda_runtime::{service_fn, LambdaEvent};
//! use magic_pocket_rs::inbound::{process_inbound_records, ReceivedMail, Receiver};
//!
//! async fn process(mail: ReceivedMail) -> Result<(), String> {
//!     // 業務処理は mail.id を一意キーにして冪等化する
//!     let recipients = mail.recipients();
//!     let _ = (recipients, mail.raw.len());
//!     Ok(())
//! }
//!
//! async fn handle(event: LambdaEvent<SqsEvent>) -> Result<SqsBatchResponse, lambda_runtime::Error> {
//!     let receiver = Receiver::from_env("inbox").await?;
//!     Ok(process_inbound_records(&receiver, event.payload, process).await)
//! }
//!
//! #[tokio::main]
//! async fn main() -> Result<(), lambda_runtime::Error> {
//!     magic_pocket_rs::set_envs().await?;
//!     lambda_runtime::run(service_fn(handle)).await
//! }
//! ```

use std::collections::HashMap;
use std::fmt::Display;
use std::future::Future;

use aws_lambda_events::event::sqs::{SqsBatchResponse, SqsEvent, SqsMessage};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tracing::{error, info};

use crate::error::{PocketError, Result};

/// SES が rule set 作成時に保存する初期設定通知の object 名。業務処理から除外する。
pub const SETUP_NOTIFICATION: &str = "AMAZON_SES_SETUP_NOTIFICATION";

const METADATA_CONTENT_TYPE: &str = "application/json";

/// SNS 再送でも変わらない、原本 object 単位の識別子 (sha256 hex、Python 側と同一)。
pub fn receipt_id(bucket: &str, key: &str) -> String {
    sha256_hex(format!("{bucket}\n{key}").as_bytes())
}

fn sha256_hex(data: &[u8]) -> String {
    hex::encode(Sha256::digest(data))
}

/// `POCKET_INBOUND` に注入される受信口 1 つ分の設定 (`InboundContext.runtime_config`)。
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct InboundConfig {
    pub region: String,
    pub bucket: String,
    pub topic_arn: String,
    pub raw_prefix: String,
    pub metadata_prefix: String,
    pub import_prefix: String,
    pub recipients: Vec<String>,
}

impl InboundConfig {
    /// `POCKET_INBOUND` から `[inbound.<name>]` の設定を取り出す。
    pub fn from_env(name: &str) -> Result<Self> {
        let raw = std::env::var("POCKET_INBOUND").map_err(|_| {
            PocketError::Inbound(
                "POCKET_INBOUND is not set (deploy された inbound handler の Lambda でのみ利用できます)"
                    .to_string(),
            )
        })?;
        Self::from_json(name, &raw)
    }

    /// `POCKET_INBOUND` と同じ JSON 文字列から取り出す (テスト・ローカル用)。
    pub fn from_json(name: &str, raw: &str) -> Result<Self> {
        let mut all: HashMap<String, InboundConfig> = serde_json::from_str(raw)?;
        all.remove(name).ok_or_else(|| {
            PocketError::Inbound(format!("inbound '{name}' は POCKET_INBOUND にありません"))
        })
    }
}

/// 保全済みの受信メール 1 件。`metadata` は `metadata/<受信ID>.json` の内容そのもの。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReceivedMail {
    /// 受信 ID ([`receipt_id`])。業務側の冪等化キーに使う
    pub id: String,
    /// MIME 原本 (S3 の raw object)
    pub raw: Vec<u8>,
    /// `{schema_version, receipt_id, source{bucket,key,version_id,sha256}, sns, ses}`
    pub metadata: Value,
}

impl ReceivedMail {
    /// SES の初期設定通知か (原本が `<raw_prefix>AMAZON_SES_SETUP_NOTIFICATION`)。
    pub fn is_setup(&self) -> bool {
        self.metadata["source"]["key"]
            .as_str()
            .is_some_and(|key| key.ends_with(&format!("/{SETUP_NOTIFICATION}")))
    }

    /// SES 通知全体 (`metadata["ses"]`)。`receipt` / `mail` を含む。
    pub fn ses(&self) -> &Value {
        &self.metadata["ses"]
    }

    /// ルールに一致した実際の受信宛先 (`ses.receipt.recipients`)。
    /// MIME の To ヘッダーや `mail.destination` とは区別して保存すること。
    pub fn recipients(&self) -> Vec<String> {
        self.metadata["ses"]["receipt"]["recipients"]
            .as_array()
            .map(|items| {
                items
                    .iter()
                    .filter_map(|v| v.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default()
    }

    /// SES の判定 (`spamVerdict` / `virusVerdict` / `spfVerdict` 等) の status。
    pub fn verdict(&self, name: &str) -> Option<&str> {
        self.metadata["ses"]["receipt"][name]["status"].as_str()
    }
}

/// S3 から取得した object。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoredObject {
    pub body: Vec<u8>,
    /// versioning が無効な bucket では None (または "null")
    pub version_id: Option<String>,
}

/// 条件付き保存の結果。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PutOutcome {
    Created,
    /// `If-None-Match: *` が 412 で拒否された (同時受信で先に保存された)
    AlreadyExists,
}

/// 原本・受信情報の保管先。本番は [`aws_sdk_s3::Client`]、テストはメモリ実装。
pub trait ObjectStore {
    /// object を取得する。存在しなければ `Ok(None)`。
    fn get(
        &self,
        bucket: &str,
        key: &str,
        version_id: Option<&str>,
    ) -> impl Future<Output = Result<Option<StoredObject>>> + Send;

    /// 同じ key が無いときだけ保存する (`If-None-Match: *`)。
    fn put_if_absent(
        &self,
        bucket: &str,
        key: &str,
        body: Vec<u8>,
        content_type: &str,
    ) -> impl Future<Output = Result<PutOutcome>> + Send;
}

impl ObjectStore for aws_sdk_s3::Client {
    async fn get(
        &self,
        bucket: &str,
        key: &str,
        version_id: Option<&str>,
    ) -> Result<Option<StoredObject>> {
        let response = self
            .get_object()
            .bucket(bucket)
            .key(key)
            .set_version_id(version_id.map(str::to_string))
            .send()
            .await;
        let output = match response {
            Ok(output) => output,
            Err(err) => {
                if err.as_service_error().is_some_and(|e| e.is_no_such_key()) {
                    return Ok(None);
                }
                return Err(PocketError::S3(format!(
                    "get_object {bucket}/{key}: {}",
                    aws_sdk_s3::error::DisplayErrorContext(&err)
                )));
            }
        };
        let version_id = output.version_id().map(str::to_string);
        let body = output
            .body
            .collect()
            .await
            .map_err(|e| PocketError::S3(format!("read body {bucket}/{key}: {e}")))?
            .into_bytes()
            .to_vec();
        Ok(Some(StoredObject { body, version_id }))
    }

    async fn put_if_absent(
        &self,
        bucket: &str,
        key: &str,
        body: Vec<u8>,
        content_type: &str,
    ) -> Result<PutOutcome> {
        let response = self
            .put_object()
            .bucket(bucket)
            .key(key)
            .content_type(content_type)
            .if_none_match("*")
            .body(aws_sdk_s3::primitives::ByteStream::from(body))
            .send()
            .await;
        match response {
            Ok(_) => Ok(PutOutcome::Created),
            Err(err) => {
                use aws_sdk_s3::error::ProvideErrorMetadata;
                let precondition_failed = err
                    .raw_response()
                    .is_some_and(|raw| raw.status().as_u16() == 412)
                    || err.meta().code() == Some("PreconditionFailed");
                if precondition_failed {
                    return Ok(PutOutcome::AlreadyExists);
                }
                Err(PocketError::S3(format!(
                    "put_object {bucket}/{key}: {}",
                    aws_sdk_s3::error::DisplayErrorContext(&err)
                )))
            }
        }
    }
}

/// `POCKET_INBOUND` から受信口を選び、1 件ずつ保全する。
pub struct Receiver<S: ObjectStore> {
    config: InboundConfig,
    store: S,
}

impl Receiver<aws_sdk_s3::Client> {
    /// `POCKET_INBOUND` の設定と、その region の S3 client で受信口を用意する。
    pub async fn from_env(name: &str) -> Result<Self> {
        let config = InboundConfig::from_env(name)?;
        let sdk_config = aws_config::defaults(aws_config::BehaviorVersion::latest())
            .region(aws_config::Region::new(config.region.clone()))
            .load()
            .await;
        Ok(Self::new(config, aws_sdk_s3::Client::new(&sdk_config)))
    }
}

impl<S: ObjectStore> Receiver<S> {
    pub fn new(config: InboundConfig, store: S) -> Self {
        Self { config, store }
    }

    pub fn config(&self) -> &InboundConfig {
        &self.config
    }

    /// SNS envelope を検証し、原本の版・hash と完全な通知を条件付き保存する。
    ///
    /// 保存済み (再送・同時受信) の場合は保存済みの版とメタデータに揃えて返す。
    pub async fn receive(&self, record: &SqsMessage) -> Result<ReceivedMail> {
        let body = record.body.as_deref().unwrap_or("");
        let envelope: Value = serde_json::from_str(body)?;
        if envelope["Type"].as_str() != Some("Notification")
            || envelope["TopicArn"].as_str() != Some(self.config.topic_arn.as_str())
        {
            return Err(inbound_error("想定外のSNS通知です"));
        }
        let message = envelope["Message"]
            .as_str()
            .ok_or_else(|| inbound_error("SNS通知にMessageがありません"))?;
        let notification: Value = serde_json::from_str(message)?;
        if notification["notificationType"].as_str() != Some("Received") {
            return Err(inbound_error("SES受信通知ではありません"));
        }
        let receipt = &notification["receipt"];
        let action = &receipt["action"];
        let bucket = action["bucketName"]
            .as_str()
            .ok_or_else(|| inbound_error("SES保存先のbucketがありません"))?;
        let key = action["objectKey"]
            .as_str()
            .ok_or_else(|| inbound_error("SES保存先のkeyがありません"))?;
        if action["type"].as_str() != Some("S3")
            || bucket != self.config.bucket
            || !key.starts_with(&self.config.raw_prefix)
            || action["topicArn"].as_str() != Some(self.config.topic_arn.as_str())
        {
            return Err(inbound_error("想定外のSES保存先です"));
        }
        let recipients: Vec<&str> = receipt["recipients"]
            .as_array()
            .map(|items| items.iter().filter_map(Value::as_str).collect())
            .unwrap_or_default();
        let is_setup = key == format!("{}{}", self.config.raw_prefix, SETUP_NOTIFICATION);
        if !is_setup
            && !recipients
                .iter()
                .any(|r| self.config.recipients.iter().any(|c| c == r))
        {
            return Err(inbound_error("受信宛先が設定と一致しません"));
        }
        let identifier = receipt_id(bucket, key);
        let metadata_key = format!("{}{identifier}.json", self.config.metadata_prefix);
        if let Some(existing) = self.metadata(&metadata_key).await? {
            return self.load(existing).await;
        }
        let object = self
            .store
            .get(bucket, key, None)
            .await?
            .ok_or_else(|| inbound_error("原本がありません"))?;
        let version = match object.version_id.as_deref() {
            Some(v) if !v.is_empty() && v != "null" => v.to_string(),
            _ => {
                return Err(inbound_error(
                    "原本バケットのversioningが有効ではありません",
                ))
            }
        };
        let metadata = json!({
            "schema_version": 1,
            "receipt_id": identifier,
            "source": {
                "bucket": bucket,
                "key": key,
                "version_id": version,
                "sha256": sha256_hex(&object.body),
            },
            "sns": envelope,
            "ses": notification,
        });
        let outcome = self
            .store
            .put_if_absent(
                &self.config.bucket,
                &metadata_key,
                serde_json::to_vec(&metadata)?,
                METADATA_CONTENT_TYPE,
            )
            .await?;
        if outcome == PutOutcome::AlreadyExists {
            // 同時受信時は先に保存された原本の版とメタデータに揃える
            let saved = self
                .metadata(&metadata_key)
                .await?
                .ok_or_else(|| inbound_error("同時保存された受信情報を取得できません"))?;
            return self.load(saved).await;
        }
        Ok(ReceivedMail {
            id: identifier,
            raw: object.body,
            metadata,
        })
    }

    /// 明示した取り込みジョブ専用。S3 へのコピーだけでは起動しない。
    pub async fn load_import(&self, manifest_key: &str) -> Result<ReceivedMail> {
        if !manifest_key.starts_with(&self.config.import_prefix)
            || !manifest_key.ends_with("/manifest.json")
        {
            return Err(inbound_error("imports内のmanifest.jsonを指定してください"));
        }
        let manifest = self
            .metadata(manifest_key)
            .await?
            .ok_or_else(|| inbound_error("取り込みmanifestがありません"))?;
        let prefix = manifest_key.trim_end_matches("manifest.json");
        let raw = self
            .store
            .get(&self.config.bucket, &format!("{prefix}raw.eml"), None)
            .await?
            .ok_or_else(|| inbound_error("取り込み原本がありません"))?
            .body;
        let metadata = self
            .metadata(&format!("{prefix}metadata.json"))
            .await?
            .ok_or_else(|| inbound_error("取り込み原本と受信情報が一致しません"))?;
        if metadata["source"]["sha256"].as_str() != Some(sha256_hex(&raw).as_str()) {
            return Err(inbound_error("取り込み原本と受信情報が一致しません"));
        }
        if manifest["receipt_id"] != metadata["receipt_id"] {
            return Err(inbound_error("取り込みmanifestの受信IDが一致しません"));
        }
        let id = receipt_id_of(&metadata)?;
        Ok(ReceivedMail { id, raw, metadata })
    }

    async fn metadata(&self, key: &str) -> Result<Option<Value>> {
        match self.store.get(&self.config.bucket, key, None).await? {
            Some(object) => Ok(Some(serde_json::from_slice(&object.body)?)),
            None => Ok(None),
        }
    }

    async fn load(&self, metadata: Value) -> Result<ReceivedMail> {
        let source = &metadata["source"];
        let bucket = source["bucket"].as_str().unwrap_or_default();
        let key = source["key"].as_str().unwrap_or_default();
        if bucket != self.config.bucket || !key.starts_with(&self.config.raw_prefix) {
            return Err(inbound_error("保存済みメタデータの原本参照が不正です"));
        }
        let version_id = source["version_id"].as_str();
        let raw = self
            .store
            .get(bucket, key, version_id)
            .await?
            .ok_or_else(|| inbound_error("保存済みメタデータの原本がありません"))?
            .body;
        if source["sha256"].as_str() != Some(sha256_hex(&raw).as_str()) {
            return Err(inbound_error("原本のhashが一致しません"));
        }
        let id = receipt_id_of(&metadata)?;
        Ok(ReceivedMail { id, raw, metadata })
    }
}

fn receipt_id_of(metadata: &Value) -> Result<String> {
    metadata["receipt_id"]
        .as_str()
        .map(str::to_string)
        .ok_or_else(|| inbound_error("受信情報にreceipt_idがありません"))
}

fn inbound_error(message: &str) -> PocketError {
    PocketError::Inbound(message.to_string())
}

/// SqsEvent の record を 1 件ずつ受信 → 保全 → `process` に渡し、失敗 record だけを
/// `batchItemFailures` として集約した partial batch response を返す。
///
/// SES の初期設定通知は保存だけして `process` には渡さない。`process` の副作用は
/// `mail.id` をキーに冪等化すること (保存は冪等だが、処理を一度だけ実行する保証は
/// ない)。`process` が `Err` を返した record は失敗として報告され再配信される。
/// ログにはエラー種別だけを出し、メール本文や通知内容は出さない。
pub async fn process_inbound_records<S, F, Fut, E>(
    receiver: &Receiver<S>,
    event: SqsEvent,
    mut process: F,
) -> SqsBatchResponse
where
    S: ObjectStore,
    F: FnMut(ReceivedMail) -> Fut,
    Fut: Future<Output = std::result::Result<(), E>>,
    E: Display,
{
    let mut response = SqsBatchResponse::default();
    for record in event.records {
        let message_id = record.message_id.clone().unwrap_or_default();
        match receiver.receive(&record).await {
            Ok(mail) => {
                if mail.is_setup() {
                    info!(
                        "inbound record {} is a setup notification (stored, skipped)",
                        message_id
                    );
                    continue;
                }
                let id = mail.id.clone();
                match process(mail).await {
                    Ok(()) => info!("inbound record {} processed (receipt {})", message_id, id),
                    Err(e) => {
                        error!(
                            "受信処理失敗: SQS ID={} / receipt {} / {}",
                            message_id, id, e
                        );
                        response.add_failure(message_id);
                    }
                }
            }
            Err(e) => {
                error!("受信処理失敗: SQS ID={} / {}", message_id, error_kind(&e));
                response.add_failure(message_id);
            }
        }
    }
    response
}

fn error_kind(error: &PocketError) -> &'static str {
    match error {
        PocketError::Inbound(_) => "Inbound",
        PocketError::S3(_) => "S3",
        PocketError::JsonParse(_) => "JsonParse",
        _ => "Other",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    const TOPIC: &str = "arn:aws:sns:ap-northeast-1:123456789012:dev-prj-pocket-inbound-inbox-abc";
    const BUCKET: &str = "dev-prj-pocket-inbound-inbox-abc-123456789012";

    fn config() -> InboundConfig {
        InboundConfig {
            region: "ap-northeast-1".into(),
            bucket: BUCKET.into(),
            topic_arn: TOPIC.into(),
            raw_prefix: "raw/".into(),
            metadata_prefix: "metadata/".into(),
            import_prefix: "imports/".into(),
            recipients: vec!["test@receive.example.com".into()],
        }
    }

    /// (version_id, body) の履歴。versioning 有効な bucket を再現する
    type Versions = Vec<(String, Vec<u8>)>;

    /// メモリ上の S3 代替。versioning は有効 (put ごとに v<n>)、`If-None-Match: *` を再現。
    #[derive(Default)]
    struct MemoryStore {
        objects: Mutex<HashMap<(String, String), Versions>>,
        puts: Mutex<Vec<String>>,
    }

    impl MemoryStore {
        fn seed(&self, key: &str, body: &[u8]) {
            let mut objects = self.objects.lock().unwrap();
            let versions = objects.entry((BUCKET.into(), key.into())).or_default();
            versions.push((format!("v{}", versions.len() + 1), body.to_vec()));
        }
    }

    impl ObjectStore for MemoryStore {
        async fn get(
            &self,
            bucket: &str,
            key: &str,
            version_id: Option<&str>,
        ) -> Result<Option<StoredObject>> {
            let objects = self.objects.lock().unwrap();
            let Some(versions) = objects.get(&(bucket.to_string(), key.to_string())) else {
                return Ok(None);
            };
            let found = match version_id {
                Some(v) => versions.iter().find(|(id, _)| id == v),
                None => versions.last(),
            };
            Ok(found.map(|(id, body)| StoredObject {
                body: body.clone(),
                version_id: Some(id.clone()),
            }))
        }

        async fn put_if_absent(
            &self,
            bucket: &str,
            key: &str,
            body: Vec<u8>,
            _content_type: &str,
        ) -> Result<PutOutcome> {
            self.puts.lock().unwrap().push(key.to_string());
            let mut objects = self.objects.lock().unwrap();
            let entry = objects
                .entry((bucket.to_string(), key.to_string()))
                .or_default();
            if !entry.is_empty() {
                return Ok(PutOutcome::AlreadyExists);
            }
            entry.push(("v1".into(), body));
            Ok(PutOutcome::Created)
        }
    }

    fn notification(key: &str, recipients: &[&str]) -> Value {
        json!({
            "notificationType": "Received",
            "mail": {"destination": ["other@receive.example.com"], "messageId": key.rsplit('/').next()},
            "receipt": {
                "recipients": recipients,
                "spamVerdict": {"status": "PASS"},
                "virusVerdict": {"status": "PASS"},
                "action": {"type": "S3", "bucketName": BUCKET, "objectKey": key, "topicArn": TOPIC},
            },
        })
    }

    fn record(topic: &str, notification: &Value) -> SqsMessage {
        let envelope = json!({
            "Type": "Notification",
            "MessageId": "sns-1",
            "TopicArn": topic,
            "Message": notification.to_string(),
        });
        serde_json::from_value(json!({
            "messageId": "msg-1",
            "receiptHandle": "rh-1",
            "body": envelope.to_string(),
        }))
        .unwrap()
    }

    fn event(records: Vec<SqsMessage>) -> SqsEvent {
        let mut event: SqsEvent = serde_json::from_value(json!({"Records": []})).unwrap();
        event.records = records;
        event
    }

    #[test]
    fn test_receipt_id_matches_python_sha256_of_bucket_newline_key() {
        // hashlib.sha256(b"b\nk").hexdigest() と同値 (Python 側と ID を共有する)
        assert_eq!(
            receipt_id("b", "k"),
            "1c2a35c609fdd46ae6cbd1e1246e357c700249928450c43a3c5ef4fa02034416"
        );
        assert_ne!(receipt_id("b", "k"), receipt_id("b", "k2"));
    }

    #[test]
    fn test_config_from_json_selects_inbound_by_name() {
        let raw = json!({"inbox": {
            "region": "ap-northeast-1", "bucket": BUCKET, "topic_arn": TOPIC,
            "raw_prefix": "raw/", "metadata_prefix": "metadata/", "import_prefix": "imports/",
            "recipients": ["test@receive.example.com"],
        }})
        .to_string();
        assert_eq!(InboundConfig::from_json("inbox", &raw).unwrap(), config());
        let err = InboundConfig::from_json("other", &raw).unwrap_err();
        assert!(err.to_string().contains("other"));
    }

    #[test]
    fn test_receive_stores_metadata_and_returns_mail() {
        let store = MemoryStore::default();
        store.seed("raw/abc", b"From: a@example.com\r\n\r\nhello");
        let receiver = Receiver::new(config(), store);
        let rt = tokio::runtime::Runtime::new().unwrap();
        let rec = record(
            TOPIC,
            &notification("raw/abc", &["test@receive.example.com"]),
        );

        let mail = rt.block_on(receiver.receive(&rec)).unwrap();

        assert_eq!(mail.id, receipt_id(BUCKET, "raw/abc"));
        assert_eq!(mail.raw, b"From: a@example.com\r\n\r\nhello");
        assert!(!mail.is_setup());
        assert_eq!(
            mail.recipients(),
            vec!["test@receive.example.com".to_string()]
        );
        assert_eq!(mail.verdict("virusVerdict"), Some("PASS"));
        assert_eq!(mail.metadata["schema_version"], 1);
        assert_eq!(mail.metadata["source"]["version_id"], "v1");
        assert_eq!(
            mail.metadata["source"]["sha256"],
            sha256_hex(mail.raw.as_slice())
        );
        assert_eq!(mail.metadata["sns"]["TopicArn"], TOPIC);
        assert_eq!(mail.metadata["ses"]["notificationType"], "Received");
        let puts = receiver.store.puts.lock().unwrap().clone();
        assert_eq!(puts, vec![format!("metadata/{}.json", mail.id)]);
    }

    #[test]
    fn test_receive_is_idempotent_and_pins_saved_version() {
        let store = MemoryStore::default();
        store.seed("raw/abc", b"first");
        let receiver = Receiver::new(config(), store);
        let rt = tokio::runtime::Runtime::new().unwrap();
        let rec = record(
            TOPIC,
            &notification("raw/abc", &["test@receive.example.com"]),
        );
        let first = rt.block_on(receiver.receive(&rec)).unwrap();
        // 原本が上書きされても (新しい版)、保存済みの版と hash に揃える
        receiver.store.seed("raw/abc", b"second");

        let again = rt.block_on(receiver.receive(&rec)).unwrap();

        assert_eq!(again, first);
        assert_eq!(again.raw, b"first");
        assert_eq!(receiver.store.puts.lock().unwrap().len(), 1);
    }

    #[test]
    fn test_receive_follows_concurrent_writer_on_precondition_failed() {
        /// 最初の metadata 取得では無く、put が 412 で拒否され、その後は別 worker の
        /// 保存内容が読める、という同時受信の順序を再現する
        struct Concurrent {
            inner: MemoryStore,
            saved: Value,
            metadata_key: String,
            get_calls: Mutex<usize>,
        }
        impl ObjectStore for Concurrent {
            async fn get(
                &self,
                bucket: &str,
                key: &str,
                version_id: Option<&str>,
            ) -> Result<Option<StoredObject>> {
                if key == self.metadata_key {
                    let mut calls = self.get_calls.lock().unwrap();
                    *calls += 1;
                    if *calls == 1 {
                        return Ok(None);
                    }
                    return Ok(Some(StoredObject {
                        body: self.saved.to_string().into_bytes(),
                        version_id: Some("v1".into()),
                    }));
                }
                self.inner.get(bucket, key, version_id).await
            }
            async fn put_if_absent(
                &self,
                _: &str,
                key: &str,
                _: Vec<u8>,
                _: &str,
            ) -> Result<PutOutcome> {
                assert_eq!(key, self.metadata_key);
                Ok(PutOutcome::AlreadyExists)
            }
        }
        let inner = MemoryStore::default();
        inner.seed("raw/abc", b"body");
        let id = receipt_id(BUCKET, "raw/abc");
        let saved = json!({
            "schema_version": 1, "receipt_id": id,
            "source": {"bucket": BUCKET, "key": "raw/abc", "version_id": "v1", "sha256": sha256_hex(b"body")},
            "sns": {"MessageId": "sns-other"}, "ses": {"receipt": {"recipients": ["test@receive.example.com"]}},
        });
        let receiver = Receiver::new(
            config(),
            Concurrent {
                inner,
                saved: saved.clone(),
                metadata_key: format!("metadata/{id}.json"),
                get_calls: Mutex::new(0),
            },
        );
        let rt = tokio::runtime::Runtime::new().unwrap();
        let rec = record(
            TOPIC,
            &notification("raw/abc", &["test@receive.example.com"]),
        );

        let mail = rt.block_on(receiver.receive(&rec)).unwrap();

        // 自分の envelope ではなく、先に保存された受信情報に揃う
        assert_eq!(mail.metadata, saved);
        assert_eq!(mail.raw, b"body");
        assert_eq!(*receiver.store.get_calls.lock().unwrap(), 2);
    }

    #[test]
    fn test_receive_rejects_foreign_topic_and_destination() {
        let store = MemoryStore::default();
        store.seed("raw/abc", b"body");
        let receiver = Receiver::new(config(), store);
        let rt = tokio::runtime::Runtime::new().unwrap();

        let foreign = record(
            "arn:aws:sns:ap-northeast-1:123456789012:other",
            &notification("raw/abc", &["test@receive.example.com"]),
        );
        let err = rt.block_on(receiver.receive(&foreign)).unwrap_err();
        assert!(err.to_string().contains("想定外のSNS通知"), "{err}");

        let mut wrong_bucket = notification("raw/abc", &["test@receive.example.com"]);
        wrong_bucket["receipt"]["action"]["bucketName"] = json!("other-bucket");
        let err = rt
            .block_on(receiver.receive(&record(TOPIC, &wrong_bucket)))
            .unwrap_err();
        assert!(err.to_string().contains("想定外のSES保存先"), "{err}");

        let mut not_received = notification("raw/abc", &["test@receive.example.com"]);
        not_received["notificationType"] = json!("Bounce");
        let err = rt
            .block_on(receiver.receive(&record(TOPIC, &not_received)))
            .unwrap_err();
        assert!(
            err.to_string().contains("SES受信通知ではありません"),
            "{err}"
        );

        let other = record(
            TOPIC,
            &notification("raw/abc", &["someone@receive.example.com"]),
        );
        let err = rt.block_on(receiver.receive(&other)).unwrap_err();
        assert!(
            err.to_string().contains("受信宛先が設定と一致しません"),
            "{err}"
        );
        // 検証で弾いた record は何も保存しない
        assert!(receiver.store.puts.lock().unwrap().is_empty());
    }

    #[test]
    fn test_setup_notification_is_stored_but_flagged() {
        let store = MemoryStore::default();
        let key = format!("raw/{SETUP_NOTIFICATION}");
        store.seed(&key, b"setup");
        let receiver = Receiver::new(config(), store);
        let rt = tokio::runtime::Runtime::new().unwrap();
        // 初期設定通知は宛先が空でも受け入れる
        let rec = record(TOPIC, &notification(&key, &[]));

        let mail = rt.block_on(receiver.receive(&rec)).unwrap();

        assert!(mail.is_setup());
        assert_eq!(receiver.store.puts.lock().unwrap().len(), 1);
    }

    #[test]
    fn test_receive_requires_versioned_bucket() {
        struct Unversioned;
        impl ObjectStore for Unversioned {
            async fn get(
                &self,
                _: &str,
                key: &str,
                _: Option<&str>,
            ) -> Result<Option<StoredObject>> {
                if key.starts_with("metadata/") {
                    return Ok(None);
                }
                Ok(Some(StoredObject {
                    body: b"x".to_vec(),
                    version_id: Some("null".into()),
                }))
            }
            async fn put_if_absent(
                &self,
                _: &str,
                _: &str,
                _: Vec<u8>,
                _: &str,
            ) -> Result<PutOutcome> {
                panic!("versioning 無しでは保存しない");
            }
        }
        let receiver = Receiver::new(config(), Unversioned);
        let rt = tokio::runtime::Runtime::new().unwrap();
        let rec = record(
            TOPIC,
            &notification("raw/abc", &["test@receive.example.com"]),
        );
        let err = rt.block_on(receiver.receive(&rec)).unwrap_err();
        assert!(err.to_string().contains("versioning"), "{err}");
    }

    #[test]
    fn test_load_import_validates_manifest_pair() {
        let store = MemoryStore::default();
        let id = receipt_id(BUCKET, "raw/abc");
        let metadata = json!({
            "schema_version": 1, "receipt_id": id,
            "source": {"bucket": BUCKET, "key": "raw/abc", "version_id": "v1", "sha256": sha256_hex(b"body")},
            "sns": {}, "ses": {"receipt": {"recipients": ["test@receive.example.com"]}},
        });
        store.seed(&format!("imports/{id}/raw.eml"), b"body");
        store.seed(
            &format!("imports/{id}/metadata.json"),
            metadata.to_string().as_bytes(),
        );
        store.seed(
            &format!("imports/{id}/manifest.json"),
            json!({"receipt_id": id}).to_string().as_bytes(),
        );
        let receiver = Receiver::new(config(), store);
        let rt = tokio::runtime::Runtime::new().unwrap();

        let mail = rt
            .block_on(receiver.load_import(&format!("imports/{id}/manifest.json")))
            .unwrap();
        assert_eq!(mail.id, id);
        assert_eq!(mail.raw, b"body");

        let err = rt.block_on(receiver.load_import("raw/abc")).unwrap_err();
        assert!(err.to_string().contains("manifest.json"), "{err}");
        let err = rt
            .block_on(receiver.load_import("imports/missing/manifest.json"))
            .unwrap_err();
        assert!(err.to_string().contains("manifest"), "{err}");
    }

    #[test]
    fn test_process_inbound_records_reports_only_failures_and_skips_setup() {
        let store = MemoryStore::default();
        store.seed("raw/ok", b"ok");
        store.seed("raw/ng", b"ng");
        store.seed(&format!("raw/{SETUP_NOTIFICATION}"), b"setup");
        let receiver = Receiver::new(config(), store);
        let rt = tokio::runtime::Runtime::new().unwrap();
        let mut ok = record(
            TOPIC,
            &notification("raw/ok", &["test@receive.example.com"]),
        );
        ok.message_id = Some("m-ok".into());
        let mut ng = record(
            TOPIC,
            &notification("raw/ng", &["test@receive.example.com"]),
        );
        ng.message_id = Some("m-ng".into());
        let mut setup = record(
            TOPIC,
            &notification(&format!("raw/{SETUP_NOTIFICATION}"), &[]),
        );
        setup.message_id = Some("m-setup".into());
        let mut invalid = record(
            "arn:aws:sns:ap-northeast-1:123456789012:other",
            &notification("raw/ok", &["test@receive.example.com"]),
        );
        invalid.message_id = Some("m-invalid".into());
        let processed = Mutex::new(Vec::new());

        let response = rt.block_on(process_inbound_records(
            &receiver,
            event(vec![ok, ng, setup, invalid]),
            |mail| {
                let processed = &processed;
                async move {
                    processed.lock().unwrap().push(mail.raw.clone());
                    if mail.raw == b"ng" {
                        Err("boom".to_string())
                    } else {
                        Ok(())
                    }
                }
            },
        ));

        let ids: Vec<_> = response
            .batch_item_failures
            .iter()
            .map(|f| f.item_identifier.as_str())
            .collect();
        assert_eq!(ids, ["m-ng", "m-invalid"]);
        // 初期設定通知は保存されるが process には渡らない
        assert_eq!(
            *processed.lock().unwrap(),
            vec![b"ok".to_vec(), b"ng".to_vec()]
        );
        assert_eq!(receiver.store.puts.lock().unwrap().len(), 3);
    }
}
