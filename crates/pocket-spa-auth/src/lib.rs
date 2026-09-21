use hmac::{Hmac, KeyInit, Mac};
use sha2::Sha256;
use std::time::{SystemTime, UNIX_EPOCH};

type HmacSha256 = Hmac<Sha256>;

/// トークンを格納する Cookie 名 (Python 側 pocket.django.spa_auth.COOKIE_NAME と同値)
pub const COOKIE_NAME: &str = "pocket-spa-token";

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
    let expiry = now_secs() + max_age_secs;
    let msg = format!("{user_id}:{expiry}");
    let mut mac = HmacSha256::new_from_slice(&secret).expect("HMAC キー長エラー");
    mac.update(msg.as_bytes());
    let sig = hex::encode(mac.finalize().into_bytes());
    Ok(format!("{user_id}:{expiry}:{sig}"))
}

/// 検証済みトークンの中身 (Python 側 pocket.django.spa_auth.VerifiedToken と対応)
///
/// sliding refresh (残り寿命が短いときの再発行) の判定に使えるよう、user_id と
/// 有効期限をまとめて返す。トークン形式 (`{user_id}:{expiry}:{hmac}`) は実装詳細
/// なので、利用側は文字列を split せずこちらを使う。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerifiedToken {
    pub user_id: String,
    /// 有効期限 (unix 秒)
    pub expires_at: u64,
}

impl VerifiedToken {
    /// 残り寿命 (秒)。呼び出し時点で期限を過ぎていれば 0。
    pub fn remaining_secs(&self) -> u64 {
        self.expires_at.saturating_sub(now_secs())
    }
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("システム時刻エラー")
        .as_secs()
}

/// トークンを検証し、有効なら [`VerifiedToken`] を返す。無効・期限切れは None。
pub fn verify_token(token: &str, secret_hex: &str) -> Option<VerifiedToken> {
    let parts: Vec<&str> = token.splitn(3, ':').collect();
    if parts.len() != 3 {
        return None;
    }
    let user_id = parts[0];
    let expiry_str = parts[1];
    let sig = parts[2];
    let expiry: u64 = expiry_str.parse().ok()?;
    if now_secs() > expiry {
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
    Some(VerifiedToken {
        user_id: user_id.to_string(),
        expires_at: expiry,
    })
}

/// ログイン用 Cookie 値を生成する
///
/// 戻り値は `Set-Cookie` ヘッダにそのまま載せることを想定した文字列。
/// axum-extra の `CookieJar` / `PrivateCookieJar` (cookie crate の `encoded()`) を
/// 経由するとトークン区切りの `:` が `%3A` に percent-encode される。pocket の
/// CloudFront Function は decode してから検証するため動作はするが、
/// 生の値を `SET_COOKIE` に append する方が確実 (KN1458)。
pub fn login_cookie_value(token: &str, max_age_secs: u64) -> String {
    format!(
        "{COOKIE_NAME}={token}; Max-Age={max_age_secs}; \
         HttpOnly; Secure; SameSite=Lax; Path=/"
    )
}

/// ログアウト用 Cookie 値を生成する
pub fn logout_cookie_value() -> String {
    format!("{COOKIE_NAME}=; Max-Age=0; HttpOnly; Secure; SameSite=Lax; Path=/")
}

#[cfg(test)]
mod tests {
    use super::*;

    const TEST_SECRET: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    #[test]
    fn test_generate_and_verify() {
        let token = generate_token("user123", TEST_SECRET, 3600).unwrap();
        let verified = verify_token(&token, TEST_SECRET).unwrap();
        assert_eq!(verified.user_id, "user123");
        // 生成直後なので残り寿命は max_age とほぼ等しい (秒境界の跨ぎを許容)
        let remaining = verified.remaining_secs();
        assert!((3599..=3600).contains(&remaining), "{remaining}");
    }

    #[test]
    fn test_remaining_secs_saturates_at_zero() {
        let verified = VerifiedToken {
            user_id: "user123".to_string(),
            expires_at: 0,
        };
        assert_eq!(verified.remaining_secs(), 0);
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

    #[test]
    fn test_python_shared_vector() {
        // Python 実装 (pocket.django.spa_auth) と同じ fixture を読み、トークン形式・
        // HMAC 計算・Cookie 名の乖離を CI で検出する。Python 側の対になるテストは
        // tests/test_spa_auth.py の test_shared_vector_with_rust
        let raw = include_str!("../../../tests/data/spa_auth_vectors.json");
        let v: serde_json::Value = serde_json::from_str(raw).unwrap();
        assert_eq!(v["cookie_name"].as_str().unwrap(), COOKIE_NAME);
        let token = v["token"].as_str().unwrap();
        let secret = v["secret_hex"].as_str().unwrap();
        // verify は期待 HMAC を再計算して比較するので、署名アルゴリズムの一致を含む
        let verified = verify_token(token, secret).unwrap();
        assert_eq!(verified.user_id, v["user_id"].as_str().unwrap());
        assert_eq!(verified.expires_at, v["expiry"].as_u64().unwrap());
    }
}
