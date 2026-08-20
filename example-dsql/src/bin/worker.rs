//! SQS worker (Lambda)。日次スケジューラが投げた message を処理する。
//!
//! 経路: EventBridge Scheduler → SQS queue → この worker → Aurora DSQL に 1 行 INSERT。
//! Lambda を直接 invoke せず queue を挟むのは、リトライ/失敗系を SQS の
//! visibility timeout / redrive (DLQ) に一元化するため。
//!
//! `process_sqs_records` は record 単位で結果を集約し、失敗した record だけを
//! `batchItemFailures` として報告する。バッチ全体を Err で落とすと、同じバッチで
//! 既に成功した record まで再配信されて二重実行になる。

use std::sync::Arc;

use aws_lambda_events::event::sqs::{SqsBatchResponse, SqsEvent};
use lambda_runtime::{service_fn, LambdaEvent};
use pocket_example_dsql::{config::AppConfig, db, db::TokenRefresher, jobs::Job, models};
use sea_orm::DatabaseConnection;

async fn handle(
    event: LambdaEvent<SqsEvent>,
    db: &DatabaseConnection,
) -> Result<SqsBatchResponse, lambda_runtime::Error> {
    Ok(
        magic_pocket_rs::sqs::process_sqs_records(event.payload, |record| async move {
            let body = record.body.as_deref().unwrap_or("");
            let job: Job = serde_json::from_str(body).map_err(|e| e.to_string())?;
            let now = chrono::Utc::now().to_rfc3339();
            // record 処理は panic ではなく Err を返す規約
            // (panic するとバッチ全体が再配信される)
            models::messages::create(db, &job.body(&now), "scheduler")
                .await
                .map_err(|e| e.to_string())?;
            Ok::<(), String>(())
        })
        .await,
    )
}

#[tokio::main]
async fn main() -> Result<(), lambda_runtime::Error> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .init();

    magic_pocket_rs::set_envs().await?; // SSM/Secrets → 環境変数

    let config = AppConfig::from_env();
    // worker も warm のまま生き続けるため refresher 付きで接続する
    // (DSQL の IAM トークンは最大 15 分)
    let (db, refresher) = db::connect_with_refresher(&config).await;
    let db = db.expect("DSQL に接続できません (POCKET_DSQL_ENDPOINT 未設定?)");
    // TokenRefresher は Clone でないので Arc に載せて invoke 間で共有する
    let refresher: Option<Arc<TokenRefresher>> = refresher.map(Arc::new);

    lambda_runtime::run(service_fn(move |event: LambdaEvent<SqsEvent>| {
        let db = db.clone();
        let refresher = refresher.clone();
        async move {
            // invoke 契機でトークンの鮮度を保つ (HTTP 側は最外層 middleware が担当)
            if let Some(refresher) = refresher.as_ref() {
                refresher.ensure_fresh(&db).await;
            }
            handle(event, &db).await
        }
    }))
    .await
}
