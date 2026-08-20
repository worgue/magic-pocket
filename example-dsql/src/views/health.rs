use serde::Serialize;

#[derive(Serialize)]
pub struct HealthView {
    pub status: &'static str,
    pub db_configured: bool,
}
