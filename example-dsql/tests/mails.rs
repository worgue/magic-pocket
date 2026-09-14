use axum::body::Body;
use axum::http::{Request, StatusCode};
use pocket_example_dsql::models::mails::parse_headers;
use tower::util::ServiceExt;

#[tokio::test]
async fn mails_without_db_is_service_unavailable() {
    let router = pocket_example_dsql::app::router(None, None);
    let response = router
        .oneshot(
            Request::builder()
                .uri("/api/mails")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
}

#[test]
fn parse_headers_reads_subject_and_from_address() {
    let raw = b"From: Alice <alice@example.com>\r\nTo: test@example.com\r\n\
Subject: =?UTF-8?B?44OG44K544OI?=\r\nMIME-Version: 1.0\r\n\
Content-Type: text/plain; charset=utf-8\r\n\r\nhello\r\n";
    let (subject, sender) = parse_headers(raw);
    assert_eq!(subject, "テスト");
    assert_eq!(sender, "alice@example.com");
}

#[test]
fn parse_headers_is_empty_for_garbage() {
    let (subject, sender) = parse_headers(b"");
    assert_eq!(subject, "");
    assert_eq!(sender, "");
}
