//! `migrations/*.sql` を接続先へ適用する最小の applier。
//!
//! 信頼の源は `schema.sql`。運用プロジェクトでは専用のマイグレーションツールを
//! 使うが、この example はテーブルが 1 つだけなので、依存を増やさず
//! SeaORM の生 SQL 実行だけで完結させている。
//!
//! - 適用済みの migration は `schema_migrations` に記録し、再実行しても飛ばす
//! - DDL は 1 文ずつ autocommit で流す (DSQL は 1 トランザクション 1 DDL)
//!
//! 接続先は通常のアプリと同じ env で解決する (`DATABASE_URL` > `PG_HOST` 系 >
//! `DSQL_HOST` / `POCKET_DSQL_ENDPOINT`)。
//!
//! ```sh
//! DSQL_HOST=<endpoint> cargo run --bin schema-apply
//! ```

use std::path::Path;

use pocket_example_dsql::{config::AppConfig, db};
use sea_orm::{ConnectionTrait, DatabaseConnection, DbBackend, Statement};

const MIGRATIONS_DIR: &str = "migrations";

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let config = AppConfig::from_env();
    let db = db::connect(&config)
        .await
        .ok_or_else(|| anyhow::anyhow!("DB 接続の env がありません (DATABASE_URL / DSQL_HOST)"))?;

    ensure_history_table(&db).await?;
    let applied = applied_migrations(&db).await?;

    let mut files: Vec<_> = std::fs::read_dir(Path::new(MIGRATIONS_DIR))?
        .filter_map(|e| e.ok())
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .filter(|name| name.ends_with(".sql"))
        .collect();
    files.sort();

    for name in files {
        if applied.contains(&name) {
            println!("skip   {name} (適用済み)");
            continue;
        }
        let sql = std::fs::read_to_string(Path::new(MIGRATIONS_DIR).join(&name))?;
        for statement in split_statements(&sql) {
            db.execute(Statement::from_string(DbBackend::Postgres, statement))
                .await?;
        }
        db.execute(Statement::from_sql_and_values(
            DbBackend::Postgres,
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            [name.clone().into()],
        ))
        .await?;
        println!("apply  {name}");
    }
    Ok(())
}

async fn ensure_history_table(db: &DatabaseConnection) -> anyhow::Result<()> {
    db.execute(Statement::from_string(
        DbBackend::Postgres,
        "CREATE TABLE IF NOT EXISTS schema_migrations (\
            name text NOT NULL, \
            applied_at timestamptz NOT NULL DEFAULT now(), \
            PRIMARY KEY (name))"
            .to_string(),
    ))
    .await?;
    Ok(())
}

async fn applied_migrations(db: &DatabaseConnection) -> anyhow::Result<Vec<String>> {
    let rows = db
        .query_all(Statement::from_string(
            DbBackend::Postgres,
            "SELECT name FROM schema_migrations",
        ))
        .await?;
    Ok(rows
        .into_iter()
        .filter_map(|r| r.try_get::<String>("", "name").ok())
        .collect())
}

/// `;` 区切りの SQL を 1 文ずつに割る。コメント行と空文は落とす。
///
/// 素朴な分割で足りるのは、この example の migration が管理下の DDL だけで
/// `;` を含む文字列リテラルを持たないため。
pub fn split_statements(sql: &str) -> Vec<String> {
    sql.split(';')
        .map(|chunk| {
            chunk
                .lines()
                .filter(|line| !line.trim_start().starts_with("--"))
                .collect::<Vec<_>>()
                .join("\n")
                .trim()
                .to_string()
        })
        .filter(|s| !s.is_empty())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::split_statements;

    #[test]
    fn コメント行と空文を落として1文ずつに割る() {
        let sql = "-- 先頭コメント\nCREATE TABLE a (id uuid);\n\n-- 途中コメント\nCREATE INDEX ASYNC i ON a (id);\n";
        let statements = split_statements(sql);
        assert_eq!(
            statements,
            vec![
                "CREATE TABLE a (id uuid)".to_string(),
                "CREATE INDEX ASYNC i ON a (id)".to_string(),
            ]
        );
    }

    #[test]
    fn 末尾セミコロンだけの入力は空になる() {
        assert!(split_statements("-- コメントだけ\n;\n").is_empty());
    }

    #[test]
    fn 実際のmigrationが1文以上に割れる() {
        let sql = std::fs::read_to_string("migrations/0001_create_messages.sql").unwrap();
        let statements = split_statements(&sql);
        assert_eq!(statements.len(), 2, "{statements:?}");
        assert!(statements[0].starts_with("CREATE TABLE messages"));
        // DSQL の二次インデックスは ASYNC が必須
        assert!(statements[1].contains("CREATE INDEX ASYNC"));
    }
}
