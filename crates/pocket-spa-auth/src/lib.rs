use hmac::{Hmac, KeyInit, Mac};
use sha2::Sha256;
use std::time::{SystemTime, UNIX_EPOCH};

type HmacSha256 = Hmac<Sha256>;

/// generate_token の入力不正 (Python 実装の ValueError と対応)
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TokenError {
    /// user_id に `:` が含まれる (トークン形式の区切りと衝突し verify で常に無効になる)
    UserIdContainsColon,
    /// secret_hex が 16 進文字列として不正
    InvalidSecretHex,
}

impl std::fmt::Display for TokenError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            TokenError::UserIdContainsColon => {
                write!(f, "user_id must not contain ':' (token format delimiter)")
            }
            TokenError::InvalidSecretHex => write!(f, "secret_hex is not a valid hex string"),
        }
    }
}

impl std::error::Error for TokenError {}

/// HMAC-SHA256 トークンを生成する。形式: {user_id}:{expiry_unix}:{hmac_hex}
pub fn generate_token(
    user_id: &str,
    secret_hex: &str,
    max_age_secs: u64,
) -> Result<String, TokenError> {
    if user_id.contains(':') {
        return Err(TokenError::UserIdContainsColon);
    }
    let secret = hex::decode(secret_hex).map_err(|_| TokenError::InvalidSecretHex)?;
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
    Ok(format!("{user_id}:{expiry}:{sig}"))
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
    let sig_bytes = hex::decode(sig).ok()?;
    let msg = format!("{user_id}:{expiry_str}");
    let mut mac = HmacSha256::new_from_slice(&secret).ok()?;
    mac.update(msg.as_bytes());
    // 定数時間比較 (Python 側の hmac.compare_digest と対応)。
    // 通常の文字列比較はタイミングサイドチャネルになる
    mac.verify_slice(&sig_bytes).ok()?;
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
        let token = generate_token("user123", TEST_SECRET, 3600).unwrap();
        let result = verify_token(&token, TEST_SECRET);
        assert_eq!(result, Some("user123".to_string()));
    }

    #[test]
    fn test_generate_rejects_colon_in_user_id() {
        // ':' はトークン形式の区切りなので混入を入力時点で弾く (Python の ValueError)
        let err = generate_token("user:123", TEST_SECRET, 3600).unwrap_err();
        assert_eq!(err, TokenError::UserIdContainsColon);
    }

    #[test]
    fn test_generate_rejects_invalid_hex_secret() {
        let err = generate_token("user123", "not-hex!", 3600).unwrap_err();
        assert_eq!(err, TokenError::InvalidSecretHex);
    }

    #[test]
    fn test_invalid_signature() {
        let token = generate_token("user123", TEST_SECRET, 3600).unwrap();
        let parts: Vec<&str> = token.splitn(3, ':').collect();
        let tampered = format!("{}:{}:{}", parts[0], parts[1], "bad_signature");
        assert_eq!(verify_token(&tampered, TEST_SECRET), None);
    }

    #[test]
    fn test_expired_token() {
        // 手動で期限切れトークンを作る
        let expired = "user123:0:deadbeef".to_string();
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
