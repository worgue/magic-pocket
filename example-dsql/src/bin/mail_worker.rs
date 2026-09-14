//! SES 受信 (inbound) worker (Lambda)。
//!
//! 経路: SES (S3Action で原本を非公開 bucket へ) → SNS → SQS queue → この worker
//!       → 要約を Aurora DSQL に 1 行 INSERT。
//!
//! `magic_pocket_rs::inbound::process_inbound_records` が SNS/SES 通知の検証と、
//! 原本の版・hash・完全な通知の `metadata/` への条件付き保存を済ませてから
//! `process` を呼ぶ。ここでは MIME から件名 / From を取り出して DB に登録するだけ。
//! 登録は受信 ID で冪等 (再送・再試行で二重登録しない)。

use std::sync::Arc;

use aws_lambda_events::event::sqs::{SqsBatchResponse, SqsEvent};
use lambda_runtime::{service_fn, LambdaEvent};
use magic_pocket_rs::inbound::{process_inbound_records, ReceivedMail, Receiver, S3Receiver};
use pocket_example_dsql::{config::AppConfig, db, db::TokenRefresher, models};
use sea_orm::DatabaseConnection;

/// pocket.toml の `[inbound.<name>]` の name
const INBOUND_NAME: &str = "inbox";

async fn process(db: &DatabaseConnection, mail: ReceivedMail) -> Result<(), String> {
    // 迷惑メール / ウイルス判定が PASS でないものは原本と受信情報だけ残して登録しない
    for verdict in ["spamVerdict", "virusVerdict"] {
        if mail.verdict(verdict) != Some("PASS") {
            tracing::warn!("receipt {} skipped: {} is not PASS", mail.id, verdict);
            return Ok(());
        }
    }
    let summary = models::mails::MailSummary::from_received(&mail);
    let created = models::mails::record(db, &summary)
        .await
        .map_err(|e| e.to_string())?;
    tracing::info!("receipt {} recorded (new={})", mail.id, created);
    Ok(())
}

async fn handle(
    event: LambdaEvent<SqsEvent>,
    receiver: &S3Receiver,
    db: &DatabaseConnection,
) -> Result<SqsBatchResponse, lambda_runtime::Error> {
    Ok(process_inbound_records(receiver, event.payload, |mail| process(db, mail)).await)
}

#[tokio::main]
async fn main() -> Result<(), lambda_runtime::Error> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .init();

    magic_pocket_rs::set_envs().await?; // SSM/Secrets → 環境変数

    let receiver = Receiver::from_env(INBOUND_NAME).await?;
    let config = AppConfig::from_env();
    let (db, refresher) = db::connect_with_refresher(&config).await;
    let db = db.expect("DSQL に接続できません (POCKET_DSQL_ENDPOINT 未設定?)");
    let refresher: Option<Arc<TokenRefresher>> = refresher.map(Arc::new);
    let receiver = Arc::new(receiver);

    lambda_runtime::run(service_fn(move |event: LambdaEvent<SqsEvent>| {
        let db = db.clone();
        let refresher = refresher.clone();
        let receiver = receiver.clone();
        async move {
            if let Some(refresher) = refresher.as_ref() {
                refresher.ensure_fresh(&db).await;
            }
            handle(event, &receiver, &db).await
        }
    }))
    .await
}
