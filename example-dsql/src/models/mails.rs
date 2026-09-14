//! 受信メールの要約の登録と一覧。worker (SQS) と API (axum) の両方から使う。

use chrono::{DateTime, FixedOffset, Utc};
use magic_pocket_rs::inbound::ReceivedMail;
use sea_orm::{ActiveModelTrait, ActiveValue, DatabaseConnection, DbErr, EntityTrait, QueryOrder};

use crate::entity::mails;

/// 一覧表示に要る項目だけを原本と受信情報から取り出したもの。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MailSummary {
    pub receipt_id: String,
    pub subject: String,
    pub sender: String,
    pub recipients: Vec<String>,
    pub raw_key: String,
    pub received_at: DateTime<FixedOffset>,
}

impl MailSummary {
    /// pocket が保全した受信メールから要約を作る。
    ///
    /// 件名 / From は MIME 原本から (無ければ空)、宛先は SES 通知の
    /// `receipt.recipients` (ルールに一致した実際の宛先) から、受信時刻は SES 通知の
    /// `mail.timestamp` から取る。本文・添付はここでは読まない (信頼できない入力)。
    pub fn from_received(mail: &ReceivedMail) -> Self {
        let (subject, sender) = parse_headers(&mail.raw);
        let received_at = mail.ses()["mail"]["timestamp"]
            .as_str()
            .and_then(|s| DateTime::parse_from_rfc3339(s).ok())
            .unwrap_or_else(|| Utc::now().fixed_offset());
        Self {
            receipt_id: mail.id.clone(),
            subject,
            sender,
            recipients: mail.recipients(),
            raw_key: mail.metadata["source"]["key"]
                .as_str()
                .unwrap_or_default()
                .to_string(),
            received_at,
        }
    }
}

/// MIME 原本から (件名, From アドレス) を取り出す。解析できなければ空文字。
pub fn parse_headers(raw: &[u8]) -> (String, String) {
    let Some(message) = mail_parser::MessageParser::default().parse(raw) else {
        return (String::new(), String::new());
    };
    let subject = message.subject().unwrap_or_default().to_string();
    let sender = message
        .from()
        .and_then(|from| from.first())
        .and_then(|addr| addr.address())
        .unwrap_or_default()
        .to_string();
    (subject, sender)
}

/// 要約を 1 件登録する。既に同じ受信 ID があれば何もしない (再送・再試行で二重登録
/// しない)。DSQL は UPSERT を持たないため SELECT → INSERT で冪等化する。
/// 戻り値は新規登録したかどうか。
pub async fn record(db: &DatabaseConnection, summary: &MailSummary) -> Result<bool, DbErr> {
    if mails::Entity::find_by_id(summary.receipt_id.clone())
        .one(db)
        .await?
        .is_some()
    {
        return Ok(false);
    }
    mails::ActiveModel {
        receipt_id: ActiveValue::Set(summary.receipt_id.clone()),
        subject: ActiveValue::Set(summary.subject.clone()),
        sender: ActiveValue::Set(summary.sender.clone()),
        recipients: ActiveValue::Set(
            serde_json::to_string(&summary.recipients).unwrap_or_else(|_| "[]".to_string()),
        ),
        raw_key: ActiveValue::Set(summary.raw_key.clone()),
        received_at: ActiveValue::Set(summary.received_at),
        ..Default::default()
    }
    .insert(db)
    .await?;
    Ok(true)
}

/// 受信メールの要約を新しい順で返す (messages と同じくページングなし)。
pub async fn list(db: &DatabaseConnection) -> Result<Vec<mails::Model>, DbErr> {
    mails::Entity::find()
        .order_by_desc(mails::Column::ReceivedAt)
        .all(db)
        .await
}
