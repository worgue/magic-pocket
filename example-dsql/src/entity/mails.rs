//! `mails` テーブル (schema.sql が信頼の源)。

use sea_orm::entity::prelude::*;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, DeriveEntityModel, Eq, Serialize, Deserialize)]
#[sea_orm(table_name = "mails")]
pub struct Model {
    /// pocket の受信 ID (原本 object 単位の sha256)。再送でも変わらない
    #[sea_orm(primary_key, auto_increment = false)]
    pub receipt_id: String,
    pub subject: String,
    pub sender: String,
    /// ルールに一致した実際の受信宛先 (JSON 配列の文字列)
    pub recipients: String,
    pub raw_key: String,
    pub received_at: DateTimeWithTimeZone,
    pub created_at: DateTimeWithTimeZone,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}
