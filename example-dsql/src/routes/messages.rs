use axum::extract::State;
use axum::http::StatusCode;
use axum::Json;

use crate::app::AppState;
use crate::models;
use crate::views::messages::{MessageListView, MessageView};

/// 登録済み message の一覧。日次スケジューラ (SQS 経由) が 1 日 1 件追加する。
pub async fn list(
    State(state): State<AppState>,
) -> Result<Json<MessageListView>, (StatusCode, String)> {
    let db = state
        .db
        .as_ref()
        .ok_or((StatusCode::SERVICE_UNAVAILABLE, "DB 未設定".to_string()))?;
    let rows = models::messages::list(db)
        .await
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    let messages: Vec<MessageView> = rows.into_iter().map(Into::into).collect();
    Ok(Json(MessageListView {
        count: messages.len(),
        messages,
    }))
}
