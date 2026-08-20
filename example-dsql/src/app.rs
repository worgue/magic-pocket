use std::sync::Arc;

use axum::extract::{Request, State};
use axum::middleware::{self, Next};
use axum::response::Response;
use axum::{routing::get, Router};
use sea_orm::DatabaseConnection;

use crate::db::TokenRefresher;
use crate::routes;

/// アプリ状態。db は接続系 env が無いローカル起動を許容するため Option。
#[derive(Clone)]
pub struct AppState {
    pub db: Option<DatabaseConnection>,
    /// DSQL の IAM トークン鮮度維持 (DSQL 接続時のみ Some)
    pub refresher: Option<Arc<TokenRefresher>>,
}

/// Router 構築。全 API を /api prefix 配下に置く (SPA は CloudFront の
/// default route、API は /api/* を Lambda に振る構成)。
pub fn router(db: Option<DatabaseConnection>, refresher: Option<TokenRefresher>) -> Router {
    let state = AppState {
        db,
        refresher: refresher.map(Arc::new),
    };
    Router::new()
        .route("/api/health", get(routes::health::health))
        .route("/api/messages", get(routes::messages::list))
        // DSQL トークンの鮮度維持は「最外層」に置く (DB を触る layer より外側。
        // session layer 等を足すときもこの .layer より前 = 内側に追加すること)
        .layer(middleware::from_fn_with_state(
            state.clone(),
            pre_request_maintenance,
        ))
        .with_state(state)
}

/// リクエスト契機の保守処理。DSQL 接続時はトークンの鮮度を保つ
async fn pre_request_maintenance(
    State(state): State<AppState>,
    req: Request,
    next: Next,
) -> Response {
    if let (Some(db), Some(refresher)) = (&state.db, &state.refresher) {
        refresher.ensure_fresh(db).await;
    }
    next.run(req).await
}
