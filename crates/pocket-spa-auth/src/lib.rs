use hmac::{Hmac, Mac};
use sha2::Sha256;
use std::time::{SystemTime, UNIX_EPOCH};

type HmacSha256 = Hmac<Sha256>;

/// HMAC-SHA256 トークンを生成する。形式: {user_id}:{expiry_unix}:{hmac_hex}
pub fn generate_token(user_id: &str, secret_hex: &str, max_age_secs: u64) -> String {
    let secret = hex::decode(secret_hex).expect("secret_hex が不正です");
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("システム時刻エラー")
        .as_secs();
    let expiry = now + max_age_secs;
    let msg = format!("{user_id}:{expiry}");
    let mut mac =
        HmacSha256::new_from_slice(&secret).expect("HMAC キー長エラー");
    mac.update(msg.as_bytes());
    let sig = hex::encode(mac.finalize().into_bytes());
    format!("{user_id}:{expiry}:{sig}")
}

/// トークンを検証し、有効なら user_id を返す。無効なら None。
pub fn verify_token(token: &str, secret_hex: &str) -> Option<String> {
    let parts: Vec<&str> = token.splitn(3, ':').collect();
    if parts.len() != 3 {
        return None;
    }
    let user_id = parts[0];
    let expiry_str = parts[1];
    let sig = parts[2];
    let expiry: u64 = expiry_str.parse().ok()?;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("システム時刻エラー")
        .as_secs();
    if now > expiry {
        return None;
    }
    let secret = hex::decode(secret_hex).ok()?;
    let msg = format!("{user_id}:{expiry_str}");
    let mut mac = HmacSha256::new_from_slice(&secret).ok()?;
    mac.update(msg.as_bytes());
    let expected = hex::encode(mac.finalize().into_bytes());
    if sig != expected {
        return None;
    }
    Some(user_id.to_string())
}

/// ログイン用 Cookie 値を生成する
pub fn login_cookie_value(token: &str, max_age_secs: u64) -> String {
    format!(
        "pocket-spa-token={token}; Max-Age={max_age_secs}; \
         HttpOnly; Secure; SameSite=Lax; Path=/"
    )
}

/// ログアウト用 Cookie 値を生成する
pub fn logout_cookie_value() -> String {
    "pocket-spa-token=; Max-Age=0; HttpOnly; Secure; SameSite=Lax; Path=/".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    const TEST_SECRET: &str =
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    #[test]
    fn test_generate_and_verify() {
        let token = generate_token("user123", TEST_SECRET, 3600);
        let result = verify_token(&token, TEST_SECRET);
        assert_eq!(result, Some("user123".to_string()));
    }

    #[test]
    fn test_invalid_signature() {
        let token = generate_token("user123", TEST_SECRET, 3600);
        let parts: Vec<&str> = token.splitn(3, ':').collect();
        let tampered = format!("{}:{}:{}", parts[0], parts[1], "bad_signature");
        assert_eq!(verify_token(&tampered, TEST_SECRET), None);
    }

    #[test]
    fn test_expired_token() {
        // 手動で期限切れトークンを作る
        let expired = format!("user123:0:deadbeef");
        assert_eq!(verify_token(&expired, TEST_SECRET), None);
    }

    #[test]
    fn test_malformed_token() {
        assert_eq!(verify_token("invalid", TEST_SECRET), None);
        assert_eq!(verify_token("a:b", TEST_SECRET), None);
        assert_eq!(verify_token("", TEST_SECRET), None);
    }

    #[test]
    fn test_login_cookie_value() {
        let token = "user:123:abc";
        let cookie = login_cookie_value(token, 604800);
        assert!(cookie.contains("pocket-spa-token=user:123:abc"));
        assert!(cookie.contains("Max-Age=604800"));
        assert!(cookie.contains("HttpOnly"));
    }

    #[test]
    fn test_logout_cookie_value() {
        let cookie = logout_cookie_value();
        assert!(cookie.contains("Max-Age=0"));
    }
}
