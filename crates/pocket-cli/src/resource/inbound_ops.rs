//! `pocket resource inbound --stage … --name … copy / import / redrive` (Python 版
//! `inbound_cli.py` の `copy` / `import` / `redrive`)。
//!
//! 順序が安全装置になっているので変えない:
//! - `copy`: 原本と受信情報を検証してから書き、manifest を最後に書く (manifest が揃う
//!   まで取り込みを始めない)。コピー元は読むだけで減らさない
//! - `redrive`: 保存形式を検証してから受信 queue へ送り、送信が成功したものだけ DLQ から
//!   消す。検証に落ちた message は動かさずに止める
//!
//! Python 版との違い: 受信情報・DLQ の body が JSON でない (または必要な項目が無い) とき、
//! Python 版は traceback で落ちる。Rust 版は検証に落ちたものとして同じ文言で止める
//! (どちらも何も書かず、何も消さない)

use std::time::Duration;

use anyhow::{bail, Context as _, Result};
use aws_config::SdkConfig;
use magic_pocket_settings::context::{Context, InboundContext};
use magic_pocket_settings::pyjson;
use serde_json::{json, Value as Json};
use sha2::{Digest, Sha256};

use crate::aws_util::{regional_config, sdk_error};
use crate::stack::ops::{stack_outputs, CfnClient, StackApi as _};
use crate::stack::StackOutputs;
use crate::status::sink::Sink;

/// DLQ から受け取った message
#[derive(Debug, Clone, PartialEq)]
pub struct QueueMessage {
    pub body: String,
    pub receipt_handle: String,
}

/// copy / import / redrive が呼ぶ AWS の API
pub trait InboundOps {
    async fn get_object(
        &self,
        bucket: &str,
        key: &str,
        version_id: Option<&str>,
    ) -> Result<Vec<u8>>;
    async fn put_object(
        &self,
        bucket: &str,
        key: &str,
        body: &[u8],
        content_type: &str,
    ) -> Result<()>;
    /// 同期実行し、`FunctionError` を返す (成功なら `None`)
    async fn invoke(&self, function_name: &str, payload: &[u8]) -> Result<Option<String>>;
    /// 1 件受け取る (見えなくする時間は 60 秒)。空なら `None`
    async fn receive(&self, queue_url: &str) -> Result<Option<QueueMessage>>;
    async fn send(&self, queue_url: &str, body: &str) -> Result<()>;
    async fn delete_message(&self, queue_url: &str, receipt_handle: &str) -> Result<()>;
}

/// Python の `validate_receipt_id`
fn validate_receipt_id(value: &str) -> Result<()> {
    let ok = value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b));
    if !ok {
        bail!("受信IDは64桁のSHA256です");
    }
    Ok(())
}

fn output<'o>(outputs: &'o StackOutputs, key: &str) -> Result<&'o str> {
    outputs
        .get(key)
        .map(String::as_str)
        .with_context(|| format!("受信スタックの出力 {key} がありません"))
}

/// コピー元の受信情報が指す原本
struct SourceRef {
    bucket: String,
    key: String,
    version_id: String,
    sha256: String,
}

fn source_ref(metadata: &Json) -> Option<SourceRef> {
    let source = metadata.get("source")?;
    let text = |name: &str| source.get(name)?.as_str().map(str::to_string);
    Some(SourceRef {
        bucket: text("bucket")?,
        key: text("key")?,
        version_id: text("version_id")?,
        sha256: text("sha256")?,
    })
}

/// `copy`: 1 件の原本と受信情報を別 stage の `imports/` へコピーし、manifest を最後に書く
#[allow(clippy::too_many_arguments)]
pub async fn copy(
    source_ops: &impl InboundOps,
    target_ops: &impl InboundOps,
    source: &InboundContext,
    source_outputs: Option<&StackOutputs>,
    target: &InboundContext,
    target_outputs: Option<&StackOutputs>,
    identifier: &str,
    sink: &mut dyn Sink,
) -> Result<()> {
    let (Some(source_outputs), Some(target_outputs)) = (source_outputs, target_outputs) else {
        bail!("コピー元と先の受信スタックが必要です");
    };
    let source_bucket = output(source_outputs, "Bucket")?;
    let target_bucket = output(target_outputs, "Bucket")?;
    let config = &source.config;
    let key = format!("{}{identifier}.json", config.metadata_prefix);
    let metadata_bytes = source_ops.get_object(source_bucket, &key, None).await?;
    let metadata: Json = serde_json::from_slice(&metadata_bytes).unwrap_or(Json::Null);
    let reference = source_ref(&metadata)
        .filter(|r| r.bucket == source_bucket && r.key.starts_with(&config.raw_prefix));
    let Some(reference) = reference else {
        bail!("原本の参照先が設定と一致しません");
    };
    let raw = source_ops
        .get_object(
            &reference.bucket,
            &reference.key,
            Some(&reference.version_id),
        )
        .await?;
    if metadata.get("receipt_id").and_then(Json::as_str) != Some(identifier)
        || hex::encode(Sha256::digest(&raw)) != reference.sha256
    {
        bail!("原本のhashまたは受信IDが一致しません");
    }
    // 同じ受信 ID の copy は同じ内容になる。manifest が揃うまで解析ジョブを投入しない
    let prefix = format!("{}{identifier}/", target.config.import_prefix);
    let manifest = pyjson::dumps(&json!({"schema_version": 1, "receipt_id": identifier}));
    let files: [(&str, &[u8], &str); 3] = [
        ("raw.eml", &raw, "message/rfc822"),
        ("metadata.json", &metadata_bytes, "application/json"),
        ("manifest.json", manifest.as_bytes(), "application/json"),
    ];
    for (suffix, body, content_type) in files {
        target_ops
            .put_object(
                target_bucket,
                &format!("{prefix}{suffix}"),
                body,
                content_type,
            )
            .await?;
    }
    sink.print(&format!("{prefix}manifest.json"));
    sink.print("コピー完了。取り込み用handlerからReceiver.load_importで明示的に解析してください。");
    Ok(())
}

/// `import`: コピー済みの manifest を、受信 worker と同じ container の別 handler で同期実行する
pub async fn import(
    ops: &impl InboundOps,
    context: &Context,
    inlet: &InboundContext,
    manifest_key: &str,
    handler_ref: &str,
    sink: &mut dyn Sink,
) -> Result<()> {
    let function_name = resolve_handler(context, handler_ref)?;
    let (container, _) = split_handler(handler_ref)?;
    let (receiving_container, _) = split_handler(&inlet.config.handler)?;
    if container != receiving_container || handler_ref == inlet.config.handler {
        bail!("受信workerと同じcontainerの別handlerを指定してください");
    }
    if !manifest_key.starts_with(&inlet.config.import_prefix)
        || !manifest_key.ends_with("/manifest.json")
    {
        bail!("imports内のmanifest.jsonを指定してください");
    }
    let payload = pyjson::dumps(&json!({"inbound": inlet.name, "manifest_key": manifest_key}));
    if ops
        .invoke(&function_name, payload.as_bytes())
        .await?
        .is_some()
    {
        bail!("取り込みhandlerが失敗しました。Lambdaログを確認してください");
    }
    sink.print("取り込みhandlerの実行が完了しました。");
    Ok(())
}

/// Python の `parse_handler_ref`
fn split_handler(reference: &str) -> Result<(&str, &str)> {
    match reference.split_once('.') {
        Some(parts) => Ok(parts),
        None => bail!(
            "handler 参照 '{reference}' には container 名が必要です。 \
             \"<container>.<handler>\" のドット記法で指定してください \
             (例: handler = \"mydjango.wsgi\")。"
        ),
    }
}

/// Python の `Settings.resolve_handler`。Lambda の関数名を返す
fn resolve_handler(context: &Context, reference: &str) -> Result<String> {
    let (container, key) = split_handler(reference)?;
    let Some(found) = context.container.get(container) else {
        bail!(
            "handler '{reference}': container '{container}' が [container.<name>] にありません。"
        );
    };
    let Some(handler) = found.handlers.get(key) else {
        bail!("handler '{reference}': container '{container}' に handler '{key}' がありません。");
    };
    Ok(handler.function_name.clone())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, clap::ValueEnum)]
pub enum RedriveQueue {
    Delivery,
    Worker,
}

const UNEXPECTED_ENVELOPE: &str = "想定外の通知形式です。DLQから削除せず中止します";
const UNEXPECTED_DESTINATION: &str = "想定外のSES保存先です。DLQから削除せず中止します";

/// DLQ の message が受信口の SES 通知 (SNS 経由) か
fn check_message(inlet: &InboundContext, outputs: &StackOutputs, body: &str) -> Result<()> {
    let envelope: Json = serde_json::from_str(body).unwrap_or(Json::Null);
    let topic = outputs.get("TopicArn").map(String::as_str);
    if envelope.get("Type").and_then(Json::as_str) != Some("Notification")
        || envelope.get("TopicArn").and_then(Json::as_str) != topic
    {
        bail!(UNEXPECTED_ENVELOPE);
    }
    let Some(notification) = envelope
        .get("Message")
        .and_then(Json::as_str)
        .and_then(|message| serde_json::from_str::<Json>(message).ok())
    else {
        bail!(UNEXPECTED_ENVELOPE);
    };
    let action = &notification["receipt"]["action"];
    let text = |value: &Json| value.as_str().map(str::to_string);
    if text(&notification["notificationType"]).as_deref() != Some("Received")
        || text(&action["type"]).as_deref() != Some("S3")
        || text(&action["bucketName"]).as_deref() != outputs.get("Bucket").map(String::as_str)
        || !text(&action["objectKey"])
            .unwrap_or_default()
            .starts_with(&inlet.config.raw_prefix)
    {
        bail!(UNEXPECTED_DESTINATION);
    }
    Ok(())
}

/// `redrive`: DLQ の message を検証して受信 queue へ戻す。送信に成功したものだけ消す
pub async fn redrive(
    ops: &impl InboundOps,
    inlet: &InboundContext,
    outputs: Option<&StackOutputs>,
    queue: RedriveQueue,
    limit: u32,
    sink: &mut dyn Sink,
) -> Result<()> {
    let Some(outputs) = outputs else {
        bail!("受信スタックがありません");
    };
    let source = output(
        outputs,
        match queue {
            RedriveQueue::Delivery => "DeliveryDLQUrl",
            RedriveQueue::Worker => "WorkerDLQUrl",
        },
    )?;
    let destination = output(outputs, "QueueUrl")?;
    let mut count = 0;
    for _ in 0..limit {
        let Some(message) = ops.receive(source).await? else {
            break;
        };
        check_message(inlet, outputs, &message.body)?;
        ops.send(destination, &message.body).await?;
        ops.delete_message(source, &message.receipt_handle).await?;
        count += 1;
    }
    sink.print(&format!(
        "{count}件を受信queueへ再投入しました。重複実行に備え業務側で冪等化してください。"
    ));
    Ok(())
}

// ---- AWS ----

pub struct AwsInboundOps {
    s3: aws_sdk_s3::Client,
    lambda: aws_sdk_lambda::Client,
    sqs: aws_sdk_sqs::Client,
}

impl AwsInboundOps {
    pub async fn new(sdk: &SdkConfig, region: &str) -> Self {
        // 取り込み handler は最長 15 分動く。再試行すると二重に取り込むので 1 回だけ
        let lambda_config = aws_sdk_lambda::config::Builder::from(&regional_config(sdk, region))
            .timeout_config(
                aws_config::timeout::TimeoutConfig::builder()
                    .connect_timeout(Duration::from_secs(10))
                    .read_timeout(Duration::from_secs(900))
                    .build(),
            )
            .retry_config(aws_config::retry::RetryConfig::standard().with_max_attempts(1))
            .build();
        Self {
            s3: crate::s3_client(region).await,
            lambda: aws_sdk_lambda::Client::from_conf(lambda_config),
            sqs: aws_sdk_sqs::Client::new(&regional_config(sdk, region)),
        }
    }
}

impl InboundOps for AwsInboundOps {
    async fn get_object(
        &self,
        bucket: &str,
        key: &str,
        version_id: Option<&str>,
    ) -> Result<Vec<u8>> {
        let response = self
            .s3
            .get_object()
            .bucket(bucket)
            .key(key)
            .set_version_id(version_id.map(str::to_string))
            .send()
            .await
            .map_err(|e| sdk_error("s3 get_object", &e))?;
        let body = response
            .body
            .collect()
            .await
            .context("s3 get_object の本文を読めません")?;
        Ok(body.into_bytes().to_vec())
    }

    async fn put_object(
        &self,
        bucket: &str,
        key: &str,
        body: &[u8],
        content_type: &str,
    ) -> Result<()> {
        self.s3
            .put_object()
            .bucket(bucket)
            .key(key)
            .body(body.to_vec().into())
            .content_type(content_type)
            .send()
            .await
            .map_err(|e| sdk_error("s3 put_object", &e))?;
        Ok(())
    }

    async fn invoke(&self, function_name: &str, payload: &[u8]) -> Result<Option<String>> {
        let response = self
            .lambda
            .invoke()
            .function_name(function_name)
            .invocation_type(aws_sdk_lambda::types::InvocationType::RequestResponse)
            .payload(aws_sdk_lambda::primitives::Blob::new(payload))
            .send()
            .await
            .map_err(|e| sdk_error("lambda invoke", &e))?;
        Ok(response.function_error().map(str::to_string))
    }

    async fn receive(&self, queue_url: &str) -> Result<Option<QueueMessage>> {
        let response = self
            .sqs
            .receive_message()
            .queue_url(queue_url)
            .max_number_of_messages(1)
            .visibility_timeout(60)
            .send()
            .await
            .map_err(|e| sdk_error("sqs receive_message", &e))?;
        Ok(response.messages().first().map(|message| QueueMessage {
            body: message.body().unwrap_or_default().to_string(),
            receipt_handle: message.receipt_handle().unwrap_or_default().to_string(),
        }))
    }

    async fn send(&self, queue_url: &str, body: &str) -> Result<()> {
        self.sqs
            .send_message()
            .queue_url(queue_url)
            .message_body(body)
            .send()
            .await
            .map_err(|e| sdk_error("sqs send_message", &e))?;
        Ok(())
    }

    async fn delete_message(&self, queue_url: &str, receipt_handle: &str) -> Result<()> {
        self.sqs
            .delete_message()
            .queue_url(queue_url)
            .receipt_handle(receipt_handle)
            .send()
            .await
            .map_err(|e| sdk_error("sqs delete_message", &e))?;
        Ok(())
    }
}

/// 受信 stack の出力 (無い・空なら `None`)
pub async fn inbound_outputs(
    sdk: &SdkConfig,
    inlet: &InboundContext,
) -> Result<Option<StackOutputs>> {
    let description = CfnClient::new(sdk, &inlet.region)
        .describe(&inlet.resource_name)
        .await?;
    Ok(stack_outputs(description.as_ref()))
}

/// copy の入口 (コピー先の stage の Context を作る)
pub async fn run_copy(
    sdk: &SdkConfig,
    source: &InboundContext,
    identifier: &str,
    to_stage: &str,
    to_name: &str,
    sink: &mut dyn Sink,
) -> Result<()> {
    validate_receipt_id(identifier)?;
    let (target_context, _) = crate::context::load_context_read_only(to_stage)?;
    let Some(target) = target_context.inbound.get(to_name) else {
        bail!("コピー先のinbound.{to_name}がありません");
    };
    let source_outputs = inbound_outputs(sdk, source).await?;
    let target_outputs = inbound_outputs(sdk, target).await?;
    let source_ops = AwsInboundOps::new(sdk, &source.region).await;
    let target_ops = AwsInboundOps::new(sdk, &target.region).await;
    copy(
        &source_ops,
        &target_ops,
        source,
        source_outputs.as_ref(),
        target,
        target_outputs.as_ref(),
        identifier,
        sink,
    )
    .await
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;
    use std::collections::{BTreeMap, BTreeSet, VecDeque};

    use super::*;
    use crate::deploy::poll::testing::run;
    use crate::stack::testing::vector_context;
    use crate::status::sink::Recorded;

    #[derive(Default)]
    struct FakeOps {
        /// (bucket, key) → 本文
        objects: RefCell<BTreeMap<(String, String), Vec<u8>>>,
        /// 書き込みの順
        puts: RefCell<Vec<String>>,
        /// この key の put を失敗させる
        fail_put: Option<String>,
        /// queue URL → message
        queues: RefCell<BTreeMap<String, VecDeque<QueueMessage>>>,
        /// 何件目 (1 始まり) の送信を失敗させるか
        fail_send_at: Option<usize>,
        /// 受け取り済み (見えない) の receipt handle
        taken: RefCell<BTreeSet<String>>,
        sent: RefCell<Vec<(String, String)>>,
        invoked: RefCell<Vec<(String, String)>>,
        function_error: Option<String>,
    }

    impl FakeOps {
        fn put(&self, bucket: &str, key: &str, body: &[u8]) {
            self.objects
                .borrow_mut()
                .insert((bucket.to_string(), key.to_string()), body.to_vec());
        }

        fn queue(&self, url: &str) -> Vec<String> {
            self.queues
                .borrow()
                .get(url)
                .map(|q| q.iter().map(|m| m.body.clone()).collect())
                .unwrap_or_default()
        }
    }

    impl InboundOps for FakeOps {
        async fn get_object(
            &self,
            bucket: &str,
            key: &str,
            _version_id: Option<&str>,
        ) -> Result<Vec<u8>> {
            match self
                .objects
                .borrow()
                .get(&(bucket.to_string(), key.to_string()))
            {
                Some(body) => Ok(body.clone()),
                None => bail!("s3 get_object: NoSuchKey: {key}"),
            }
        }

        async fn put_object(
            &self,
            bucket: &str,
            key: &str,
            body: &[u8],
            content_type: &str,
        ) -> Result<()> {
            if self.fail_put.as_deref() == Some(key) {
                bail!("s3 put_object: InternalError: {key}");
            }
            self.puts.borrow_mut().push(format!("{key} {content_type}"));
            self.put(bucket, key, body);
            Ok(())
        }

        async fn invoke(&self, function_name: &str, payload: &[u8]) -> Result<Option<String>> {
            self.invoked.borrow_mut().push((
                function_name.to_string(),
                String::from_utf8(payload.to_vec()).unwrap(),
            ));
            Ok(self.function_error.clone())
        }

        async fn receive(&self, queue_url: &str) -> Result<Option<QueueMessage>> {
            // 受け取ったものは見えなくなる (消すのは delete_message)
            let queues = self.queues.borrow();
            let mut taken = self.taken.borrow_mut();
            let found = queues
                .get(queue_url)
                .and_then(|q| q.iter().find(|m| !taken.contains(&m.receipt_handle)))
                .cloned();
            if let Some(message) = found.as_ref() {
                taken.insert(message.receipt_handle.clone());
            }
            Ok(found)
        }

        async fn send(&self, queue_url: &str, body: &str) -> Result<()> {
            if self.fail_send_at == Some(self.sent.borrow().len() + 1) {
                bail!("sqs send_message: ServiceUnavailable: retry");
            }
            self.sent
                .borrow_mut()
                .push((queue_url.to_string(), body.to_string()));
            Ok(())
        }

        async fn delete_message(&self, queue_url: &str, receipt_handle: &str) -> Result<()> {
            if let Some(queue) = self.queues.borrow_mut().get_mut(queue_url) {
                queue.retain(|m| m.receipt_handle != receipt_handle);
            }
            Ok(())
        }
    }

    fn context() -> Context {
        vector_context("example/example-dsql.sandbox")
    }

    fn outputs(bucket: &str) -> StackOutputs {
        let mut map = StackOutputs::new();
        for (key, value) in [
            ("Bucket", bucket),
            ("TopicArn", "arn:topic"),
            ("QueueUrl", "q-main"),
            ("DeliveryDLQUrl", "q-delivery-dlq"),
            ("WorkerDLQUrl", "q-worker-dlq"),
        ] {
            map.insert(key.to_string(), value.to_string());
        }
        map
    }

    const RAW: &[u8] = b"From: a@example.com\r\n\r\nhello";

    /// コピー元に原本と受信情報を置き、受信 ID を返す
    fn seed_source(ops: &FakeOps, inlet: &InboundContext) -> String {
        let raw_key = format!("{}abc", inlet.config.raw_prefix);
        let identifier = magic_pocket_rs::inbound::receipt_id("src-bucket", &raw_key);
        ops.put("src-bucket", &raw_key, RAW);
        let metadata = json!({
            "receipt_id": identifier,
            "source": {
                "bucket": "src-bucket",
                "key": raw_key,
                "version_id": "v1",
                "sha256": hex::encode(Sha256::digest(RAW)),
            },
        });
        ops.put(
            "src-bucket",
            &format!("{}{identifier}.json", inlet.config.metadata_prefix),
            metadata.to_string().as_bytes(),
        );
        identifier
    }

    fn do_copy(ops: &FakeOps, identifier: &str) -> (Result<()>, Recorded) {
        let ctx = context();
        let inlet = ctx.inbound.values().next().unwrap();
        let mut sink = Recorded::default();
        let result = run(copy(
            ops,
            ops,
            inlet,
            Some(&outputs("src-bucket")),
            inlet,
            Some(&outputs("dst-bucket")),
            identifier,
            &mut sink,
        ));
        (result, sink)
    }

    #[test]
    fn test_validate_receipt_id() {
        assert!(validate_receipt_id(&"a".repeat(64)).is_ok());
        for bad in [
            "A".repeat(64),
            "a".repeat(63),
            format!("{}g", "a".repeat(63)),
        ] {
            assert_eq!(
                validate_receipt_id(&bad).unwrap_err().to_string(),
                "受信IDは64桁のSHA256です"
            );
        }
    }

    #[test]
    fn test_copy_writes_manifest_last_and_keeps_source() {
        let ctx = context();
        let inlet = ctx.inbound.values().next().unwrap();
        let ops = FakeOps::default();
        let identifier = seed_source(&ops, inlet);
        let before = ops.objects.borrow().clone();
        let (result, sink) = do_copy(&ops, &identifier);
        result.unwrap();
        let prefix = format!("{}{identifier}/", inlet.config.import_prefix);
        assert_eq!(
            *ops.puts.borrow(),
            [
                format!("{prefix}raw.eml message/rfc822"),
                format!("{prefix}metadata.json application/json"),
                format!("{prefix}manifest.json application/json"),
            ]
        );
        let objects = ops.objects.borrow();
        assert_eq!(
            objects[&("dst-bucket".to_string(), format!("{prefix}manifest.json"))],
            format!("{{\"schema_version\": 1, \"receipt_id\": \"{identifier}\"}}").as_bytes()
        );
        assert_eq!(
            objects[&("dst-bucket".to_string(), format!("{prefix}raw.eml"))],
            RAW
        );
        for (key, body) in &before {
            assert_eq!(objects.get(key), Some(body), "コピー元は変えない");
        }
        assert_eq!(sink.0[0].2, format!("{prefix}manifest.json"));
    }

    #[test]
    fn test_copy_failure_never_writes_manifest_before_the_rest() {
        let ctx = context();
        let inlet = ctx.inbound.values().next().unwrap();
        let mut ops = FakeOps::default();
        let identifier = seed_source(&ops, inlet);
        let prefix = format!("{}{identifier}/", inlet.config.import_prefix);
        ops.fail_put = Some(format!("{prefix}metadata.json"));
        let (result, _) = do_copy(&ops, &identifier);
        assert!(result.unwrap_err().to_string().contains("InternalError"));
        assert!(!ops
            .objects
            .borrow()
            .contains_key(&("dst-bucket".to_string(), format!("{prefix}manifest.json"))));
        // 再実行で揃う (同じ受信 ID の copy は同じ内容)
        ops.fail_put = None;
        do_copy(&ops, &identifier).0.unwrap();
        assert!(ops
            .objects
            .borrow()
            .contains_key(&("dst-bucket".to_string(), format!("{prefix}manifest.json"))));
    }

    #[test]
    fn test_copy_rejects_mismatched_source() {
        let ctx = context();
        let inlet = ctx.inbound.values().next().unwrap();
        let metadata_key = |id: &str| format!("{}{id}.json", inlet.config.metadata_prefix);
        // hash が一致しない
        let ops = FakeOps::default();
        let identifier = seed_source(&ops, inlet);
        ops.put(
            "src-bucket",
            &format!("{}abc", inlet.config.raw_prefix),
            b"tampered",
        );
        let (result, _) = do_copy(&ops, &identifier);
        assert_eq!(
            result.unwrap_err().to_string(),
            "原本のhashまたは受信IDが一致しません"
        );
        assert!(ops.puts.borrow().is_empty());
        // 参照先が別の bucket / JSON でない受信情報
        for metadata in [
            json!({"receipt_id": identifier, "source": {"bucket": "other", "key": "raw/abc", "version_id": "v", "sha256": "x"}})
                .to_string(),
            "not json".to_string(),
        ] {
            ops.put("src-bucket", &metadata_key(&identifier), metadata.as_bytes());
            let (result, _) = do_copy(&ops, &identifier);
            assert_eq!(
                result.unwrap_err().to_string(),
                "原本の参照先が設定と一致しません"
            );
            assert!(ops.puts.borrow().is_empty());
        }
        // 受信スタックが無い
        let mut sink = Recorded::default();
        let error = run(copy(
            &ops,
            &ops,
            inlet,
            None,
            inlet,
            Some(&outputs("d")),
            &identifier,
            &mut sink,
        ))
        .unwrap_err();
        assert_eq!(error.to_string(), "コピー元と先の受信スタックが必要です");
    }

    #[test]
    fn test_import_invokes_other_handler_of_receiving_container() {
        let ctx = context();
        let inlet = ctx.inbound.values().next().unwrap();
        let (container, _) = split_handler(&inlet.config.handler).unwrap();
        let receiving = ctx.container.get(container).unwrap();
        let other = receiving
            .handlers
            .keys()
            .find(|key| format!("{container}.{key}") != inlet.config.handler)
            .unwrap()
            .clone();
        let handler_ref = format!("{container}.{other}");
        let manifest = format!("{}abc/manifest.json", inlet.config.import_prefix);
        let ops = FakeOps::default();
        let mut sink = Recorded::default();
        run(import(
            &ops,
            &ctx,
            inlet,
            &manifest,
            &handler_ref,
            &mut sink,
        ))
        .unwrap();
        assert_eq!(
            *ops.invoked.borrow(),
            [(
                receiving
                    .handlers
                    .get(&other)
                    .unwrap()
                    .function_name
                    .clone(),
                format!(
                    "{{\"inbound\": \"{}\", \"manifest_key\": \"{manifest}\"}}",
                    inlet.name
                )
            )]
        );
        assert_eq!(sink.0[0].2, "取り込みhandlerの実行が完了しました。");

        for (handler, key, message) in [
            (
                inlet.config.handler.as_str(),
                manifest.as_str(),
                "受信workerと同じcontainerの別handlerを指定してください",
            ),
            (
                handler_ref.as_str(),
                "raw/abc/manifest.json",
                "imports内のmanifest.jsonを指定してください",
            ),
            (
                "nope.mail",
                manifest.as_str(),
                "handler 'nope.mail': container 'nope' が [container.<name>] にありません。",
            ),
        ] {
            let error = run(import(&ops, &ctx, inlet, key, handler, &mut sink)).unwrap_err();
            assert_eq!(error.to_string(), message);
        }
        assert_eq!(ops.invoked.borrow().len(), 1, "検査に落ちたら呼ばない");

        let ops = FakeOps {
            function_error: Some("Unhandled".to_string()),
            ..Default::default()
        };
        let error = run(import(
            &ops,
            &ctx,
            inlet,
            &manifest,
            &handler_ref,
            &mut sink,
        ))
        .unwrap_err();
        assert_eq!(
            error.to_string(),
            "取り込みhandlerが失敗しました。Lambdaログを確認してください"
        );
    }

    fn notification(bucket: &str, key: &str, handle: &str) -> QueueMessage {
        let message = json!({
            "notificationType": "Received",
            "receipt": {"action": {"type": "S3", "bucketName": bucket, "objectKey": key}},
        });
        QueueMessage {
            body: json!({
                "Type": "Notification",
                "TopicArn": "arn:topic",
                "Message": message.to_string(),
                "MessageId": handle,
            })
            .to_string(),
            receipt_handle: handle.to_string(),
        }
    }

    fn fill(ops: &FakeOps, url: &str, messages: Vec<QueueMessage>) {
        ops.queues
            .borrow_mut()
            .insert(url.to_string(), messages.into_iter().collect());
    }

    fn do_redrive(ops: &FakeOps, limit: u32) -> (Result<()>, Recorded) {
        let ctx = context();
        let inlet = ctx.inbound.values().next().unwrap();
        let mut sink = Recorded::default();
        let result = run(redrive(
            ops,
            inlet,
            Some(&outputs("bk")),
            RedriveQueue::Delivery,
            limit,
            &mut sink,
        ));
        (result, sink)
    }

    #[test]
    fn test_redrive_moves_valid_messages_up_to_limit() {
        let ctx = context();
        let raw = ctx
            .inbound
            .values()
            .next()
            .unwrap()
            .config
            .raw_prefix
            .clone();
        let ops = FakeOps::default();
        let messages: Vec<QueueMessage> = (1..=3)
            .map(|i| notification("bk", &format!("{raw}m{i}"), &format!("h{i}")))
            .collect();
        fill(&ops, "q-delivery-dlq", messages.clone());
        let (result, sink) = do_redrive(&ops, 2);
        result.unwrap();
        assert_eq!(ops.queue("q-delivery-dlq"), [messages[2].body.clone()]);
        assert_eq!(
            sink.0[0].2,
            "2件を受信queueへ再投入しました。重複実行に備え業務側で冪等化してください。"
        );
        // 空なら何も動かさない
        let ops = FakeOps::default();
        let (result, sink) = do_redrive(&ops, 10);
        result.unwrap();
        assert!(ops.sent.borrow().is_empty());
        assert!(sink.0[0].2.starts_with("0件を受信queueへ"));
    }

    #[test]
    fn test_redrive_keeps_dlq_message_when_send_fails_or_format_is_unexpected() {
        let ctx = context();
        let raw = ctx
            .inbound
            .values()
            .next()
            .unwrap()
            .config
            .raw_prefix
            .clone();
        // 2 件目の送信が失敗 → 1 件目だけ消え、2 件目は DLQ に残る
        let ops = FakeOps {
            fail_send_at: Some(2),
            ..Default::default()
        };
        let messages = vec![
            notification("bk", &format!("{raw}m1"), "h1"),
            notification("bk", &format!("{raw}m2"), "h2"),
        ];
        fill(&ops, "q-delivery-dlq", messages.clone());
        let (result, _) = do_redrive(&ops, 10);
        assert!(result
            .unwrap_err()
            .to_string()
            .contains("ServiceUnavailable"));
        assert_eq!(ops.queue("q-delivery-dlq"), [messages[1].body.clone()]);

        // 保存形式の検証に落ちたら送らず消さずに止める
        let bad_topic = QueueMessage {
            body: messages[0].body.replace("arn:topic", "arn:other"),
            receipt_handle: "h".to_string(),
        };
        for (message, error) in [
            (bad_topic, UNEXPECTED_ENVELOPE),
            (
                QueueMessage {
                    body: "not json".to_string(),
                    receipt_handle: "h".to_string(),
                },
                UNEXPECTED_ENVELOPE,
            ),
            (
                notification("other-bucket", &format!("{raw}m1"), "h"),
                UNEXPECTED_DESTINATION,
            ),
            (
                notification("bk", "elsewhere/m1", "h"),
                UNEXPECTED_DESTINATION,
            ),
        ] {
            let ops = FakeOps::default();
            fill(&ops, "q-delivery-dlq", vec![message.clone()]);
            let (result, _) = do_redrive(&ops, 10);
            assert_eq!(result.unwrap_err().to_string(), error);
            assert!(ops.sent.borrow().is_empty());
            assert_eq!(ops.queue("q-delivery-dlq"), [message.body]);
        }
        // 受信スタックが無い
        let inlet = ctx.inbound.values().next().unwrap();
        let error = run(redrive(
            &FakeOps::default(),
            inlet,
            None,
            RedriveQueue::Worker,
            1,
            &mut Recorded::default(),
        ))
        .unwrap_err();
        assert_eq!(error.to_string(), "受信スタックがありません");
    }
}
