use axum::extract::State;
use axum::http::StatusCode;
use axum::Json;

use crate::app::AppState;
use crate::models;
use crate::views::mails::{MailListView, MailView};

/// SES で受信したメールの要約一覧。inbound worker が受信ごとに 1 件登録する。
pub async fn list(
    State(state): State<AppState>,
) -> Result<Json<MailListView>, (StatusCode, String)> {
    let db = state
        .db
        .as_ref()
        .ok_or((StatusCode::SERVICE_UNAVAILABLE, "DB 未設定".to_string()))?;
    let rows = models::mails::list(db)
        .await
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    let mails: Vec<MailView> = rows.into_iter().map(Into::into).collect();
    Ok(Json(MailListView {
        count: mails.len(),
        mails,
    }))
}
