//! Lambda エントリポイント (HTTP。lambda_http + magic-pocket-rs)。
//! Lambda Web Adapter 等の外部 Extension は不要 (lambda_http がインプロセスで処理)。

#[tokio::main]
async fn main() -> Result<(), lambda_http::Error> {
    magic_pocket_rs::set_envs().await.unwrap(); // SSM/Secrets → 環境変数

    let config = pocket_example_dsql::config::AppConfig::from_env();
    // warm Lambda はトークン期限 (15分) を超えて生きるため refresher が必須
    let (db, refresher) = pocket_example_dsql::db::connect_with_refresher(&config).await;
    let router = pocket_example_dsql::app::router(db, refresher);

    lambda_http::run(router).await
}
