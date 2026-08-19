//! origin 直叩きを弾き、CloudFront 経由のときだけ真の client IP を取り出す
//! origin verify の Rust 実装。
//!
//! Python 側 `pocket.django.origin_verify.OriginVerifyMiddleware` /
//! `pocket.django.client_ip.parse_viewer_ip` と同仕様:
//!
//! - env secret (`POCKET_ORIGIN_VERIFY_SECRET`) が未設定 → **no-op**
//!   (local / dev で CloudFront 無し)
//! - env secret あり + `x-pocket-origin-verify` header が一致 → CloudFront 経由と
//!   みなし、`x-pocket-viewer-ip` をパースして [`ClientIp`] extension を挿入する
//! - env secret あり + header が無い / 不一致 → **origin 直叩き**なので 403
//!
//! middleware ([`origin_verify_middleware`]) は feature `axum` で有効になる。
//! `REMOTE_ADDR` を読む既存資産より前に走らせる Python 版と同じく、
//! client IP を参照する layer より外側 (Router の `.layer()` は後着が外側) に置く:
//!
//! ```ignore
//! use axum::{routing::get, Extension, Router};
//! use magic_pocket_rs::origin_verify::{origin_verify_middleware, ClientIp};
//!
//! async fn handler(ip: Option<Extension<ClientIp>>) -> String {
//!     ip.map(|Extension(ClientIp(ip))| ip.to_string())
//!         .unwrap_or_else(|| "unknown".to_string())
//! }
//!
//! let app: Router = Router::new()
//!     .route("/", get(handler))
//!     .layer(axum::middleware::from_fn(origin_verify_middleware));
//! ```

use std::net::IpAddr;

pub use crate::config::ORIGIN_VERIFY_SECRET_KEY;

/// CloudFront origin custom header 名。cloudfront.yaml の OriginCustomHeaders /
/// Python 側 ORIGIN_VERIFY_HEADER_META と一致させること。
pub const ORIGIN_VERIFY_HEADER: &str = "x-pocket-origin-verify";

/// CloudFront Function が載せる viewer IP header 名。cf_function_api_host.js /
/// Python 側 VIEWER_IP_HEADER_META と一致させること。
pub const VIEWER_IP_HEADER: &str = "x-pocket-viewer-ip";

/// origin verify を通過したリクエストの詐称不可の client IP
/// (Django 版が REMOTE_ADDR に上書きする値の axum 表現)。
///
/// `Extension<ClientIp>` extractor か `Request::extensions()` で取り出す。
/// origin verify 無効時 (env secret 未設定) や viewer IP header が
/// パース不能のときは挿入されない。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ClientIp(pub IpAddr);

/// CloudFront 由来の viewer IP header から IP 部分を取り出して検証する。
///
/// Python 側 `pocket.django.client_ip.parse_viewer_ip` と同仕様。magic-pocket の
/// CloudFront Function は `event.viewer.ip` (port 無しの素の IP) を載せるため
/// 通常 port 分解は不要だが、`CloudFront-Viewer-Address` (`IP:port`) を直接転送
/// する構成にも備えて「最後のコロンの後ろ = port」規則で頑健にパースする。
///
/// - IPv4:              `198.51.100.10`      -> `198.51.100.10`
/// - IPv4 (port 付き):   `198.51.100.10:443`  -> `198.51.100.10`
/// - IPv6 (素):          `2001:db8::1`        -> `2001:db8::1`
/// - IPv6 (角括弧+port):  `[2001:db8::1]:8080` -> `2001:db8::1`
/// - IPv6 (角括弧なし+port、CloudFront 非標準): `2001:db8::1:60776` -> `2001:db8::1`
pub fn parse_viewer_ip(value: &str) -> Option<IpAddr> {
    let value = value.trim();
    if value.is_empty() {
        return None;
    }

    // 1) 角括弧つき IPv6: [addr]:port または [addr]
    if let Some(rest) = value.strip_prefix('[') {
        return validated(rest.split(']').next().unwrap_or(""));
    }

    // 2) port 無しでそのまま妥当な IP ならそれを採用 (素の IPv4 / IPv6)
    if let Some(ip) = validated(value) {
        return Some(ip);
    }

    // 3) "最後のコロンの後ろ = port" とみなして左側を IP 候補にする
    //    (IPv4:port / 角括弧なし IPv6 + port をカバー)
    if let Some((left, _port)) = value.rsplit_once(':') {
        return validated(left);
    }
    None
}

fn validated(addr: &str) -> Option<IpAddr> {
    addr.trim().parse().ok()
}

/// `axum::middleware::from_fn` に渡す origin verify middleware。
///
/// 挙動はモジュール docs を参照。secret の比較は定数時間で行う
/// (Python 版の `hmac.compare_digest` と対応)。
#[cfg(feature = "axum")]
pub async fn origin_verify_middleware(
    mut req: axum::extract::Request,
    next: axum::middleware::Next,
) -> axum::response::Response {
    use axum::response::IntoResponse;
    use subtle::ConstantTimeEq;

    let secret = match std::env::var(ORIGIN_VERIFY_SECRET_KEY) {
        Ok(v) if !v.is_empty() => v,
        // origin verify 無効 (local/dev)。no-op
        _ => return next.run(req).await,
    };
    let provided = req
        .headers()
        .get(ORIGIN_VERIFY_HEADER)
        .map(|v| v.as_bytes())
        .unwrap_or(b"");
    if provided.ct_eq(secret.as_bytes()).unwrap_u8() == 0 {
        return (
            axum::http::StatusCode::FORBIDDEN,
            "origin direct access is not allowed",
        )
            .into_response();
    }
    let viewer = req
        .headers()
        .get(VIEWER_IP_HEADER)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if let Some(ip) = parse_viewer_ip(viewer) {
        req.extensions_mut().insert(ClientIp(ip));
    }
    next.run(req).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_viewer_ip_ipv4() {
        assert_eq!(
            parse_viewer_ip("198.51.100.10"),
            Some("198.51.100.10".parse().unwrap())
        );
        assert_eq!(
            parse_viewer_ip("198.51.100.10:443"),
            Some("198.51.100.10".parse().unwrap())
        );
        assert_eq!(
            parse_viewer_ip("  198.51.100.10  "),
            Some("198.51.100.10".parse().unwrap())
        );
    }

    #[test]
    fn test_parse_viewer_ip_ipv6() {
        let expected: IpAddr = "2001:db8::1".parse().unwrap();
        assert_eq!(parse_viewer_ip("2001:db8::1"), Some(expected));
        assert_eq!(parse_viewer_ip("[2001:db8::1]:8080"), Some(expected));
        assert_eq!(parse_viewer_ip("[2001:db8::1]"), Some(expected));
        // 角括弧なし + port (CloudFront 非標準): 最後のコロンの後ろを port とみなす
        assert_eq!(parse_viewer_ip("2001:db8::1:60776"), Some(expected));
    }

    #[test]
    fn test_parse_viewer_ip_invalid() {
        assert_eq!(parse_viewer_ip(""), None);
        assert_eq!(parse_viewer_ip("   "), None);
        assert_eq!(parse_viewer_ip("garbage"), None);
        assert_eq!(parse_viewer_ip("300.1.2.3"), None);
        assert_eq!(parse_viewer_ip(":443"), None);
        assert_eq!(parse_viewer_ip("[not-an-ip]:80"), None);
    }
}

#[cfg(all(test, feature = "axum"))]
mod middleware_tests {
    use super::*;
    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use axum::{routing::get, Extension, Router};
    use tower::ServiceExt;

    /// env はプロセス全体で共有されテストは並列実行されるため、
    /// POCKET_ORIGIN_VERIFY_SECRET を触るテストはこの lock で直列化する
    /// (lib.rs の ENV_LOCK と同じパターン。await 越しに保持しないよう
    /// block_on で駆動する)。
    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    async fn show_ip(ip: Option<Extension<ClientIp>>) -> String {
        match ip {
            Some(Extension(ClientIp(ip))) => ip.to_string(),
            None => "none".to_string(),
        }
    }

    fn app() -> Router {
        Router::new()
            .route("/", get(show_ip))
            .layer(axum::middleware::from_fn(origin_verify_middleware))
    }

    fn request(headers: &[(&str, &str)]) -> Request<Body> {
        let mut builder = Request::builder().uri("/");
        for (k, v) in headers {
            builder = builder.header(*k, *v);
        }
        builder.body(Body::empty()).unwrap()
    }

    async fn call(headers: &[(&str, &str)]) -> (StatusCode, String) {
        let resp = app().oneshot(request(headers)).await.unwrap();
        let status = resp.status();
        let body = axum::body::to_bytes(resp.into_body(), usize::MAX)
            .await
            .unwrap();
        (status, String::from_utf8_lossy(&body).to_string())
    }

    fn with_secret<F: FnOnce()>(secret: Option<&str>, f: F) {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        unsafe {
            match secret {
                Some(s) => std::env::set_var(ORIGIN_VERIFY_SECRET_KEY, s),
                None => std::env::remove_var(ORIGIN_VERIFY_SECRET_KEY),
            }
        }
        f();
        unsafe {
            std::env::remove_var(ORIGIN_VERIFY_SECRET_KEY);
        }
    }

    #[test]
    fn test_noop_without_env_secret() {
        with_secret(None, || {
            let rt = tokio::runtime::Runtime::new().unwrap();
            // header の有無に関わらず素通し。ClientIp も挿入されない
            let (status, body) = rt.block_on(call(&[("x-pocket-viewer-ip", "198.51.100.10")]));
            assert_eq!(status, StatusCode::OK);
            assert_eq!(body, "none");
        });
    }

    #[test]
    fn test_valid_secret_normalizes_client_ip() {
        with_secret(Some("s3cret"), || {
            let rt = tokio::runtime::Runtime::new().unwrap();
            let (status, body) = rt.block_on(call(&[
                ("x-pocket-origin-verify", "s3cret"),
                ("x-pocket-viewer-ip", "198.51.100.10"),
            ]));
            assert_eq!(status, StatusCode::OK);
            assert_eq!(body, "198.51.100.10");
        });
    }

    #[test]
    fn test_valid_secret_without_parsable_viewer_ip() {
        with_secret(Some("s3cret"), || {
            let rt = tokio::runtime::Runtime::new().unwrap();
            // viewer IP がパース不能でも通す (Python 版と同じ: REMOTE_ADDR 維持)
            let (status, body) = rt.block_on(call(&[
                ("x-pocket-origin-verify", "s3cret"),
                ("x-pocket-viewer-ip", "garbage"),
            ]));
            assert_eq!(status, StatusCode::OK);
            assert_eq!(body, "none");
        });
    }

    #[test]
    fn test_wrong_or_missing_secret_forbidden() {
        with_secret(Some("s3cret"), || {
            let rt = tokio::runtime::Runtime::new().unwrap();
            let (status, body) = rt.block_on(call(&[("x-pocket-origin-verify", "wrong")]));
            assert_eq!(status, StatusCode::FORBIDDEN);
            assert_eq!(body, "origin direct access is not allowed");

            let (status, _) = rt.block_on(call(&[]));
            assert_eq!(status, StatusCode::FORBIDDEN);
        });
    }
}
