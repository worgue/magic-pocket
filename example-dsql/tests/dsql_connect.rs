//! DSQL 実接続の smoke テスト (通常の cargo test では走らない)。
//! DSQL_HOST と AWS 認証を与えて手動実行する:
//!
//! ```sh
//! DSQL_HOST=<endpoint> cargo test --test dsql_connect -- --ignored
//! ```

use sea_orm::{ConnectionTrait, DbBackend, Statement};

#[tokio::test]
#[ignore = "DSQL_HOST と AWS 認証が必要な実接続テスト"]
async fn dsql_で_select_が通る() {
    let config = pocket_example_dsql::config::AppConfig::from_env();
    assert!(
        matches!(
            config.db_source,
            Some(pocket_example_dsql::config::DbSource::Dsql { .. })
        ),
        "DSQL_HOST (または POCKET_DSQL_ENDPOINT) を設定して実行すること"
    );
    let db = pocket_example_dsql::db::connect(&config).await.unwrap();
    let row = db
        .query_one(Statement::from_string(
            DbBackend::Postgres,
            "SELECT 1 AS one",
        ))
        .await
        .unwrap()
        .unwrap();
    let one: i32 = row.try_get("", "one").unwrap();
    assert_eq!(one, 1);
}
