//! ローカル開発サーバー (axum::serve)。just app で起動する。

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .init();

    let config = pocket_example_dsql::config::AppConfig::from_env();
    // 常駐プロセスなので refresher 付きで接続する (DSQL トークン鮮度維持)
    let (db, refresher) = pocket_example_dsql::db::connect_with_refresher(&config).await;
    let router = pocket_example_dsql::app::router(db, refresher);

    // コンテナ外 (host ブラウザ) からアクセスするため 0.0.0.0 でバインドする
    let listener = tokio::net::TcpListener::bind("0.0.0.0:8000")
        .await
        .expect("0.0.0.0:8000 を bind できませんでした");
    tracing::info!("listening on http://0.0.0.0:8000");
    axum::serve(listener, router)
        .await
        .expect("サーバーの実行に失敗しました");
}
