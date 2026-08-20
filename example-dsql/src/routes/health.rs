use axum::extract::State;
use axum::Json;

use crate::app::AppState;
use crate::views::health::HealthView;

/// 死活確認。DB 配線後はここで接続確認も返す想定。
pub async fn health(State(state): State<AppState>) -> Json<HealthView> {
    Json(HealthView {
        status: "ok",
        db_configured: state.db.is_some(),
    })
}
