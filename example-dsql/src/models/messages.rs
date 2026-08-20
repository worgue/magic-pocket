//! message の読み書き。worker (SQS) と API (axum) の両方から使う。

use sea_orm::{ActiveModelTrait, ActiveValue, DatabaseConnection, DbErr, EntityTrait, QueryOrder};

use crate::entity::messages;

/// 新しい message を 1 件追加する。削除は行わない (schema.sql のコメント参照)。
pub async fn create(
    db: &DatabaseConnection,
    body: &str,
    source: &str,
) -> Result<messages::Model, DbErr> {
    messages::ActiveModel {
        body: ActiveValue::Set(body.to_string()),
        source: ActiveValue::Set(source.to_string()),
        ..Default::default()
    }
    .insert(db)
    .await
}

/// 全 message を新しい順で返す。
///
/// ページングを持たないのは意図的。DSQL の 1 クエリ 3,000 行制限に対して
/// 1 日 1 行しか増えないので、約 8.2 年ぶんは素の SELECT で収まる
/// (schema.sql の messages テーブルのコメントに根拠を書いている)。
pub async fn list(db: &DatabaseConnection) -> Result<Vec<messages::Model>, DbErr> {
    messages::Entity::find()
        .order_by_desc(messages::Column::CreatedAt)
        .all(db)
        .await
}
