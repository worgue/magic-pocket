//! pocket managed secret の値の生成 (Python 版 `Mediator._generate_*`)。
//!
//! 乱数は暗号ライブラリの CSPRNG から取る (Python は `secrets`)。値そのものは Python と
//! 一致しないので、揃えるのは書式 (長さ・文字集合・鍵の形式) だけ。

use anyhow::{anyhow, bail, Result};
use aws_lc_rs::encoding::{AsDer, Pkcs8V1Der, PublicKeyX509Der};
use aws_lc_rs::rsa::{KeyPair, KeySize};
use aws_lc_rs::signature::KeyPair as _;
use base64::Engine as _;
use magic_pocket_settings::types::{OrderedMap, StrOrInt};
use serde_json::{json, Value as Json};

/// password の既定の文字集合 (Django の SECRET_KEY と同じ)
const PASSWORD_CHARS: &str = "abcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*(-_=+)";
/// Basic 認証はブラウザで入力するので、記号と紛らわしい文字を避けた英数字
const BASIC_AUTH_CHARS: &str = "abcdefghijkmnpqrstuvwxyz23456789";
const DEFAULT_LENGTH: i64 = 16;

fn random_bytes(buffer: &mut [u8]) -> Result<()> {
    aws_lc_rs::rand::fill(buffer).map_err(|_| anyhow!("乱数を取得できません"))
}

/// `secrets.token_hex(bytes)`
pub fn token_hex(bytes: usize) -> Result<String> {
    let mut buffer = vec![0u8; bytes];
    random_bytes(&mut buffer)?;
    Ok(hex::encode(buffer))
}

/// `chars` から 1 文字ずつ一様に選んで `length` 文字にする
fn random_string(length: i64, chars: &str) -> Result<String> {
    let chars: Vec<char> = chars.chars().collect();
    if chars.is_empty() {
        bail!("chars must not be empty");
    }
    let count = chars.len() as u64;
    // 剰余の偏りを避けるため、count の倍数に収まらない値は捨てて引き直す
    let limit = (u64::from(u32::MAX) + 1) / count * count;
    let mut out = String::new();
    for _ in 0..length.max(0) {
        let index = loop {
            let mut buffer = [0u8; 4];
            random_bytes(&mut buffer)?;
            let value = u64::from(u32::from_be_bytes(buffer));
            if value < limit {
                break value % count;
            }
        };
        out.push(chars[index as usize]);
    }
    Ok(out)
}

/// Aurora PostgreSQL の master password (Python の `_generate_master_password`)。
///
/// master password には `/` `"` `@` と空白を使えない。quote の事故を避けるため `'` `\\`
/// `` ` `` も外し、英数字と限られた記号から選ぶ
pub fn master_password() -> Result<String> {
    const ALPHABET: &str = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.!#%+=";
    random_string(32, ALPHABET)
}

/// `type = "password"`。options: `length` (既定 16) / `chars`
pub fn password(options: &OrderedMap<StrOrInt>) -> Result<String> {
    let length = match options.get("length") {
        None => DEFAULT_LENGTH,
        Some(StrOrInt::Int(length)) => *length,
        Some(StrOrInt::Str(_)) => bail!("length must be integer"),
    };
    let chars = match options.get("chars") {
        None => PASSWORD_CHARS,
        Some(StrOrInt::Str(chars)) => chars,
        Some(StrOrInt::Int(_)) => bail!("chars must be string"),
    };
    random_string(length, chars)
}

/// `type = "basic_auth_credential"` の `user:pass`。
///
/// username は必須。password は options で固定値を指定でき、省略時は `length` / `chars`
/// でランダムに作る
pub fn basic_auth_credential(options: &OrderedMap<StrOrInt>) -> Result<String> {
    let username = match options.get("username") {
        Some(StrOrInt::Str(username)) if !username.is_empty() && !username.contains(':') => {
            username
        }
        _ => bail!("basic_auth_credential requires options.username (str, ':' 不可)"),
    };
    let password = match options.get("password") {
        None => {
            let length = match options.get("length") {
                None => DEFAULT_LENGTH,
                Some(StrOrInt::Int(length)) => *length,
                Some(StrOrInt::Str(_)) => bail!("length must be integer"),
            };
            let chars = match options.get("chars") {
                None => BASIC_AUTH_CHARS,
                Some(StrOrInt::Str(chars)) => chars,
                Some(StrOrInt::Int(_)) => bail!("chars must be string"),
            };
            random_string(length, chars)?
        }
        Some(StrOrInt::Str(password)) if !password.is_empty() => password.clone(),
        Some(_) => bail!("basic_auth_credential options.password must be str"),
    };
    Ok(format!("{username}:{password}"))
}

/// RSA 2048 の鍵対。`pem` は秘密鍵 (PKCS#1 の PEM)、`pub` は公開鍵
/// (SubjectPublicKeyInfo の PEM) で、どちらも PEM を base64 にした文字列
pub fn rsa_pem() -> Result<Json> {
    let failed = || anyhow!("RSA の鍵対を生成できません");
    let key_pair = KeyPair::generate(KeySize::Rsa2048).map_err(|_| failed())?;
    let pkcs8: Pkcs8V1Der = key_pair.as_der().map_err(|_| failed())?;
    let private = pkcs1_from_pkcs8(pkcs8.as_ref())?;
    let public: PublicKeyX509Der = key_pair.public_key().as_der().map_err(|_| failed())?;
    let b64 = base64::engine::general_purpose::STANDARD;
    Ok(json!({
        "pem": b64.encode(pem("RSA PRIVATE KEY", private)),
        "pub": b64.encode(pem("PUBLIC KEY", public.as_ref())),
    }))
}

/// DER の PEM 表現 (base64 を 64 桁で折り返す。末尾に改行)
fn pem(label: &str, der: &[u8]) -> String {
    let body = base64::engine::general_purpose::STANDARD.encode(der);
    let mut out = format!("-----BEGIN {label}-----\n");
    for line in body.as_bytes().chunks(64) {
        out.push_str(std::str::from_utf8(line).expect("base64 は ASCII"));
        out.push('\n');
    }
    out.push_str(&format!("-----END {label}-----\n"));
    out
}

/// DER の要素を 1 つ読み、(tag, 中身, 残り) を返す
fn read_der(input: &[u8]) -> Option<(u8, &[u8], &[u8])> {
    let (&tag, rest) = input.split_first()?;
    let (&first, rest) = rest.split_first()?;
    let (length, rest) = if first & 0x80 == 0 {
        (usize::from(first), rest)
    } else {
        let count = usize::from(first & 0x7f);
        if count == 0 || count > 4 || rest.len() < count {
            return None;
        }
        let length = rest[..count]
            .iter()
            .fold(0usize, |acc, b| (acc << 8) | usize::from(*b));
        (length, &rest[count..])
    };
    (rest.len() >= length).then(|| (tag, &rest[..length], &rest[length..]))
}

/// PKCS#8 (`PrivateKeyInfo`) から中の PKCS#1 (`RSAPrivateKey`) を取り出す。
///
/// `PrivateKeyInfo ::= SEQUENCE { version INTEGER, algorithm SEQUENCE, privateKey OCTET STRING }`
/// の `privateKey` の中身が、RSA では PKCS#1 の DER そのもの
fn pkcs1_from_pkcs8(pkcs8: &[u8]) -> Result<&[u8]> {
    const SEQUENCE: u8 = 0x30;
    const INTEGER: u8 = 0x02;
    const OCTET_STRING: u8 = 0x04;
    let parse = || {
        let (tag, body, _) = read_der(pkcs8)?;
        if tag != SEQUENCE {
            return None;
        }
        let (tag, _, rest) = read_der(body)?;
        if tag != INTEGER {
            return None;
        }
        let (tag, _, rest) = read_der(rest)?;
        if tag != SEQUENCE {
            return None;
        }
        let (tag, key, _) = read_der(rest)?;
        // 中身も SEQUENCE (RSAPrivateKey) で始まる
        (tag == OCTET_STRING && key.first() == Some(&SEQUENCE)).then_some(key)
    };
    parse().ok_or_else(|| anyhow!("RSA の秘密鍵 (PKCS#8) を読めません"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn options(pairs: &[(&str, StrOrInt)]) -> OrderedMap<StrOrInt> {
        OrderedMap(
            pairs
                .iter()
                .map(|(k, v)| (k.to_string(), v.clone()))
                .collect(),
        )
    }

    fn text(value: &str) -> StrOrInt {
        StrOrInt::Str(value.to_string())
    }

    #[test]
    fn test_password_defaults() {
        let value = password(&options(&[])).unwrap();
        assert_eq!(value.chars().count(), 16);
        assert!(value.chars().all(|c| PASSWORD_CHARS.contains(c)));
        assert_ne!(value, password(&options(&[])).unwrap());
    }

    #[test]
    fn test_password_options() {
        let value = password(&options(&[
            ("length", StrOrInt::Int(50)),
            ("chars", text("aあ")),
        ]))
        .unwrap();
        assert_eq!(value.chars().count(), 50);
        assert!(value.chars().all(|c| c == 'a' || c == 'あ'));
        // 50 文字が全部同じになる確率は無視できる
        assert!(value.contains('a') && value.contains('あ'));
        assert_eq!(
            password(&options(&[("length", StrOrInt::Int(0))])).unwrap(),
            ""
        );
    }

    #[test]
    fn test_password_rejects_wrong_option_types() {
        let error = password(&options(&[("length", text("8"))])).unwrap_err();
        assert_eq!(error.to_string(), "length must be integer");
        let error = password(&options(&[("chars", StrOrInt::Int(1))])).unwrap_err();
        assert_eq!(error.to_string(), "chars must be string");
    }

    #[test]
    fn test_token_hex() {
        let value = token_hex(32).unwrap();
        assert_eq!(value.len(), 64);
        assert!(value
            .chars()
            .all(|c| c.is_ascii_digit() || ('a'..='f').contains(&c)));
    }

    #[test]
    fn test_basic_auth_credential() {
        let generated = basic_auth_credential(&options(&[("username", text("staff"))])).unwrap();
        let (user, pass) = generated.split_once(':').unwrap();
        assert_eq!(user, "staff");
        assert_eq!(pass.len(), 16);
        assert!(pass.chars().all(|c| BASIC_AUTH_CHARS.contains(c)));

        let fixed = basic_auth_credential(&options(&[
            ("username", text("staff")),
            ("password", text("fixed")),
        ]))
        .unwrap();
        assert_eq!(fixed, "staff:fixed");

        let sized = basic_auth_credential(&options(&[
            ("username", text("staff")),
            ("length", StrOrInt::Int(4)),
            ("chars", text("x")),
        ]))
        .unwrap();
        assert_eq!(sized, "staff:xxxx");
    }

    #[test]
    fn test_basic_auth_credential_validates_options() {
        for bad in [
            options(&[]),
            options(&[("username", text(""))]),
            options(&[("username", text("a:b"))]),
            options(&[("username", StrOrInt::Int(1))]),
        ] {
            assert_eq!(
                basic_auth_credential(&bad).unwrap_err().to_string(),
                "basic_auth_credential requires options.username (str, ':' 不可)"
            );
        }
        for bad in [text(""), StrOrInt::Int(1)] {
            let error =
                basic_auth_credential(&options(&[("username", text("u")), ("password", bad)]))
                    .unwrap_err();
            assert_eq!(
                error.to_string(),
                "basic_auth_credential options.password must be str"
            );
        }
    }

    fn decode_pem(encoded: &str, label: &str) -> Vec<u8> {
        let b64 = base64::engine::general_purpose::STANDARD;
        let pem = String::from_utf8(b64.decode(encoded).unwrap()).unwrap();
        let begin = format!("-----BEGIN {label}-----\n");
        let end = format!("-----END {label}-----\n");
        assert!(pem.starts_with(&begin), "{pem}");
        assert!(pem.ends_with(&end), "{pem}");
        let body = &pem[begin.len()..pem.len() - end.len()];
        assert!(body.lines().all(|line| line.len() <= 64));
        b64.decode(body.replace('\n', "")).unwrap()
    }

    #[test]
    fn test_rsa_pem_is_pkcs1_private_and_spki_public() {
        let pair = rsa_pem().unwrap();
        let private = decode_pem(pair["pem"].as_str().unwrap(), "RSA PRIVATE KEY");
        let public = decode_pem(pair["pub"].as_str().unwrap(), "PUBLIC KEY");
        // RSAPrivateKey ::= SEQUENCE { version INTEGER (0), modulus INTEGER, ... }
        let (tag, body, rest) = read_der(&private).unwrap();
        assert_eq!((tag, rest.len()), (0x30, 0));
        let (tag, version, rest) = read_der(body).unwrap();
        assert_eq!((tag, version), (0x02, &[0u8][..]));
        let (tag, modulus, _) = read_der(rest).unwrap();
        assert_eq!(tag, 0x02);
        // 2048 bit の modulus (符号用の先頭 0 を含めて 257 byte)
        assert_eq!(modulus.len(), 257);
        // 公開鍵にも同じ modulus が入っている (秘密鍵と対になっている)
        assert!(public
            .windows(modulus.len())
            .any(|window| window == modulus));
    }

    #[test]
    fn test_read_der_rejects_truncated_input() {
        assert!(read_der(&[0x30, 0x05, 0x00]).is_none());
        assert!(read_der(&[0x30, 0x82, 0x01]).is_none());
        assert!(pkcs1_from_pkcs8(&[0x30, 0x00]).is_err());
    }
}
