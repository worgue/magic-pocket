use serde::Serialize;

use crate::entity::messages;

#[derive(Serialize)]
pub struct MessageView {
    pub id: String,
    pub body: String,
    pub source: String,
    pub created_at: String,
}

impl From<messages::Model> for MessageView {
    fn from(m: messages::Model) -> Self {
        Self {
            id: m.id.to_string(),
            body: m.body,
            source: m.source,
            created_at: m.created_at.to_rfc3339(),
        }
    }
}

#[derive(Serialize)]
pub struct MessageListView {
    pub count: usize,
    pub messages: Vec<MessageView>,
}
