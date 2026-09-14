use serde::Serialize;

use crate::entity::mails;

#[derive(Serialize)]
pub struct MailView {
    pub receipt_id: String,
    pub subject: String,
    pub sender: String,
    pub recipients: Vec<String>,
    pub raw_key: String,
    pub received_at: String,
}

impl From<mails::Model> for MailView {
    fn from(m: mails::Model) -> Self {
        Self {
            receipt_id: m.receipt_id,
            subject: m.subject,
            sender: m.sender,
            recipients: serde_json::from_str(&m.recipients).unwrap_or_default(),
            raw_key: m.raw_key,
            received_at: m.received_at.to_rfc3339(),
        }
    }
}

#[derive(Serialize)]
pub struct MailListView {
    pub count: usize,
    pub mails: Vec<MailView>,
}
