//! SQS worker / enqueue ヘルパ
//!
//! pocket の `[awscontainer.handlers.<key>] sqs = {}` は queue + DLQ +
//! EventSourceMapping を生成し、runtime は `POCKET_<KEY>_QUEUEURL` を注入する
//! (resources.rs)。本モジュールはその上の「輸送層」を提供する:
//!
//! - [`process_sqs_records`]: SqsEvent を record 単位で dispatch し、失敗 record
//!   だけを partial batch response (`batchItemFailures`) として集約する
//! - [`enqueue_json`] / [`queue_url`]: `POCKET_<KEY>_QUEUEURL` を読んで
//!   SendMessage する糖衣
//!
//! メッセージ本文の形式 (Job enum 等) と dispatch の中身はアプリ側の責務で、
//! pocket は関知しない。
//!
//! # partial batch response が既定である理由
//!
//! pocket の EventSourceMapping は `ReportBatchItemFailures` を既定で有効にする。
//! このとき handler が例外でバッチ全体を落とすと、**同じバッチで既に成功した
//! record まで再配信され、冪等でない job が二重実行される** (Python 側
//! `BaseCommandHandler` で実際に踏んで修正済みの罠)。[`process_sqs_records`] は
//! record 単位の `Result` を受けて失敗分だけを SQS に報告するので、この罠を
//! 構造的に避けられる。
//!
//! なお panic は捕捉しない (バッチ全体が再配信される)。record 処理は panic
//! ではなく `Err` を返すこと。poison message はリトライの末 DLQ に落ちる。
//!
//! # Example (worker バイナリ)
//!
//! lambda_runtime はアプリ側の依存のため、この例はコンパイル検証しない。
//!
//! ```ignore
//! use aws_lambda_events::event::sqs::{SqsBatchResponse, SqsEvent};
//! use lambda_runtime::{service_fn, LambdaEvent};
//!
//! #[derive(serde::Deserialize)]
//! #[serde(tag = "type", rename_all = "snake_case")]
//! enum Job {
//!     CleanupExpired,
//! }
//!
//! async fn handle(event: LambdaEvent<SqsEvent>) -> Result<SqsBatchResponse, lambda_runtime::Error> {
//!     Ok(magic_pocket_rs::sqs::process_sqs_records(event.payload, |record| async move {
//!         let body = record.body.as_deref().unwrap_or("");
//!         let job: Job = serde_json::from_str(body).map_err(|e| e.to_string())?;
//!         match job {
//!             Job::CleanupExpired => { /* core ロジックを呼ぶ */ }
//!         }
//!         Ok::<(), String>(())
//!     })
//!     .await)
//! }
//!
//! #[tokio::main]
//! async fn main() -> Result<(), lambda_runtime::Error> {
//!     magic_pocket_rs::set_envs().await?;
//!     lambda_runtime::run(service_fn(handle)).await
//! }
//! ```

use std::fmt::Display;
use std::future::Future;

use aws_lambda_events::event::sqs::{SqsBatchResponse, SqsEvent, SqsMessage};
use tracing::{error, info};

use crate::error::{PocketError, Result};

/// SqsEvent の record を 1 件ずつ handler に渡し、失敗 record だけを
/// `batchItemFailures` として集約した partial batch response を返す。
///
/// handler が `Err` を返した record は SQS に失敗として報告され、その record
/// **だけ**が再配信される (成功済み record は削除される)。エラー内容は
/// tracing で記録される。
pub async fn process_sqs_records<F, Fut, E>(event: SqsEvent, mut handler: F) -> SqsBatchResponse
where
    F: FnMut(SqsMessage) -> Fut,
    Fut: Future<Output = std::result::Result<(), E>>,
    E: Display,
{
    let mut response = SqsBatchResponse::default();
    for record in event.records {
        let message_id = record.message_id.clone().unwrap_or_default();
        match handler(record).await {
            Ok(()) => {
                info!("SQS record {} processed", message_id);
            }
            Err(e) => {
                error!("SQS record {} failed: {}", message_id, e);
                response.add_failure(message_id);
            }
        }
    }
    response
}

/// handler key (pocket.toml の `[awscontainer.handlers.<key>]` の key) から、
/// runtime が注入した `POCKET_<KEY>_QUEUEURL` を読む。
///
/// 未注入 (deploy 前 / sqs 未設定 / `set_envs` 未実行) はエラー。
pub fn queue_url(queue_key: &str) -> Result<String> {
    let env_key = format!("POCKET_{}_QUEUEURL", queue_key.to_uppercase());
    std::env::var(&env_key).map_err(|_| {
        PocketError::Sqs(format!(
            "{env_key} is not set. handler '{queue_key}' に sqs 設定があり、\
             set_envs() 後に呼んでいるか確認してください"
        ))
    })
}

/// message を JSON 化して handler の queue へ SendMessage し、message id を返す。
///
/// wsgi 側から worker へ job を投げる用途。message の型はアプリ側で定義する
/// (worker の deserialize と一致させる)。SQS client は呼び出しごとに生成する
/// ので、高頻度に enqueue する場合はアプリ側で `aws_sdk_sqs::Client` を保持し
/// [`queue_url`] と組み合わせて直接 SendMessage すること。
pub async fn enqueue_json<T: serde::Serialize>(queue_key: &str, message: &T) -> Result<String> {
    let url = queue_url(queue_key)?;
    let body = serde_json::to_string(message)?;
    let sdk_config = aws_config::load_defaults(aws_config::BehaviorVersion::latest()).await;
    let client = aws_sdk_sqs::Client::new(&sdk_config);
    let resp = client
        .send_message()
        .queue_url(&url)
        .message_body(body)
        .send()
        .await
        .map_err(|e| {
            PocketError::Sqs(format!(
                "send_message failed for {}: {}",
                url,
                aws_sdk_sqs::error::DisplayErrorContext(&e)
            ))
        })?;
    Ok(resp.message_id().unwrap_or_default().to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn event_from_bodies(bodies: &[&str]) -> SqsEvent {
        // SqsEvent は non_exhaustive のため、実際の Lambda event と同じ JSON から
        // deserialize して組み立てる
        let records: Vec<serde_json::Value> = bodies
            .iter()
            .enumerate()
            .map(|(i, body)| {
                serde_json::json!({
                    "messageId": format!("msg-{i}"),
                    "receiptHandle": format!("rh-{i}"),
                    "body": body,
                })
            })
            .collect();
        serde_json::from_value(serde_json::json!({ "Records": records })).unwrap()
    }

    #[test]
    fn test_process_sqs_records_reports_only_failures() {
        let event = event_from_bodies(&["ok", "fail", "ok"]);
        let rt = tokio::runtime::Runtime::new().unwrap();
        let response = rt.block_on(process_sqs_records(event, |record| async move {
            match record.body.as_deref() {
                Some("fail") => Err("boom".to_string()),
                _ => Ok(()),
            }
        }));
        let ids: Vec<_> = response
            .batch_item_failures
            .iter()
            .map(|f| f.item_identifier.as_str())
            .collect();
        assert_eq!(ids, ["msg-1"]);
    }

    #[test]
    fn test_process_sqs_records_empty_on_all_success() {
        let event = event_from_bodies(&["a", "b"]);
        let rt = tokio::runtime::Runtime::new().unwrap();
        let response = rt.block_on(process_sqs_records(event, |_| async {
            Ok::<(), String>(())
        }));
        assert!(response.batch_item_failures.is_empty());
        // ReportBatchItemFailures の応答形式 (camelCase) で serialize されること
        let json = serde_json::to_value(&response).unwrap();
        assert_eq!(json["batchItemFailures"], serde_json::json!([]));
    }

    #[test]
    fn test_queue_url_reads_env() {
        // 他テストとの env 競合を避けるため専用 key を使う
        unsafe {
            std::env::set_var("POCKET_TESTSQSWORKER_QUEUEURL", "https://sqs.example.com/q");
        }
        assert_eq!(
            queue_url("testsqsworker").unwrap(),
            "https://sqs.example.com/q"
        );
        unsafe {
            std::env::remove_var("POCKET_TESTSQSWORKER_QUEUEURL");
        }
    }

    #[test]
    fn test_queue_url_missing_env_is_error() {
        let err = queue_url("no_such_worker").unwrap_err();
        assert!(err.to_string().contains("POCKET_NO_SUCH_WORKER_QUEUEURL"));
    }
}
