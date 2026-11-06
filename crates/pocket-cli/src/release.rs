//! S3 に置く release 表と current ポインタ (Python の `pocket.static.release` と同じ形)。
//!
//! route ごとの metadata prefix の下に次の 2 つを置く。rel_key は route の key prefix からの
//! 相対 key で、origin_path に依存しない。
//!
//! - `releases/<release id>.json.gz`: `{"version": 1, "release": id, "route": .., "entries": {path: rel_key}}`
//! - `current`: `{"version": 1, "release": id, "map_sha256": ..., "published_at": ...}`

use std::io::{Read, Write};
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{bail, Context, Result};
use serde::Deserialize;
use serde_json::{json, Map, Value};

use crate::mapfile::StaticMap;

pub const RELEASE_VERSION: u64 = 1;

pub fn release_key(meta_prefix: &str, release_id: &str) -> String {
    format!("{meta_prefix}releases/{release_id}.json.gz")
}

pub fn current_key(meta_prefix: &str) -> String {
    format!("{meta_prefix}current")
}

/// Python の `json.dumps(sort_keys=True)` と同じ並びになるよう、キーは辞書順に入れる
pub fn build_release_document(static_map: &StaticMap) -> Value {
    let mut entries = Map::new();
    for e in static_map.sorted_entries() {
        entries.insert(e.path.clone(), Value::String(e.rel_key()));
    }
    let mut doc = Map::new();
    doc.insert("entries".into(), Value::Object(entries));
    doc.insert("release".into(), Value::String(static_map.release_id()));
    doc.insert("route".into(), Value::String(static_map.route.clone()));
    doc.insert("version".into(), json!(RELEASE_VERSION));
    Value::Object(doc)
}

/// `json.dumps(ensure_ascii=False, sort_keys=True, separators=(",", ":"))` → gzip (mtime=0)
pub fn encode_release(document: &Value) -> Result<Vec<u8>> {
    let text = serde_json::to_vec(document)?;
    let mut encoder = flate2::GzBuilder::new()
        .mtime(0)
        .write(Vec::new(), flate2::Compression::default());
    encoder.write_all(&text)?;
    Ok(encoder.finish()?)
}

#[derive(Debug, Deserialize)]
pub struct ReleaseDocument {
    pub version: u64,
    pub release: String,
    pub entries: Map<String, Value>,
}

pub fn decode_release(data: &[u8]) -> Result<ReleaseDocument> {
    let mut json = Vec::new();
    flate2::read::GzDecoder::new(data)
        .read_to_end(&mut json)
        .context("release 表を展開できません")?;
    let doc: ReleaseDocument = serde_json::from_slice(&json).context("release 表を読めません")?;
    check_version(doc.version, "release")?;
    Ok(doc)
}

pub fn build_current_document(static_map: &StaticMap, published_at: &str) -> Value {
    let mut doc = Map::new();
    doc.insert(
        "map_sha256".into(),
        Value::String(static_map.content_sha256()),
    );
    doc.insert(
        "published_at".into(),
        Value::String(published_at.to_string()),
    );
    doc.insert("release".into(), Value::String(static_map.release_id()));
    doc.insert("version".into(), json!(RELEASE_VERSION));
    Value::Object(doc)
}

/// `json.dumps(sort_keys=True, indent=2) + "\n"` と同じ本文
pub fn encode_current(document: &Value) -> Result<Vec<u8>> {
    let mut text = serde_json::to_string_pretty(document)?;
    text.push('\n');
    Ok(text.into_bytes())
}

#[derive(Debug, Deserialize)]
pub struct CurrentDocument {
    pub version: u64,
    pub release: String,
    #[serde(default)]
    pub published_at: Option<String>,
}

pub fn decode_current(data: &[u8]) -> Result<CurrentDocument> {
    let doc: CurrentDocument = serde_json::from_slice(data).context("current を読めません")?;
    check_version(doc.version, "current")?;
    Ok(doc)
}

fn check_version(version: u64, what: &str) -> Result<()> {
    if version != RELEASE_VERSION {
        bail!(
            "{what} の version {version} は読めません (対応: {RELEASE_VERSION})。pocket の版を揃えてください"
        );
    }
    Ok(())
}

/// Python の `datetime.now(timezone.utc).isoformat(timespec="seconds")` と同じ形
/// (`2026-11-06T12:34:56+00:00`)。chrono を足さずに UNIX 秒から civil date を求める
pub fn now_iso8601_utc() -> String {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    format_iso8601_utc(secs)
}

fn format_iso8601_utc(unix_secs: i64) -> String {
    let days = unix_secs.div_euclid(86_400);
    let secs_of_day = unix_secs.rem_euclid(86_400);
    // civil_from_days (Howard Hinnant)
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}+00:00",
        secs_of_day / 3600,
        (secs_of_day % 3600) / 60,
        secs_of_day % 60
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::mapfile::MapEntry;

    fn sample() -> StaticMap {
        let mut m = StaticMap::new("web/static");
        m.add(MapEntry {
            path: "img/a.jpg".into(),
            sha256: "a".repeat(64),
            source: "data".into(),
            hashed: true,
        })
        .unwrap();
        m
    }

    #[test]
    fn release_document_roundtrip_and_key_order() {
        let doc = build_release_document(&sample());
        let text = serde_json::to_string(&doc).unwrap();
        // Python の json.dumps(sort_keys=True, separators=(",", ":")) と同じ本文
        assert_eq!(
            text,
            format!(
                "{{\"entries\":{{\"img/a.jpg\":\"img/a.aaaaaaaaaaaaaaaa.jpg\"}},\"release\":\"{}\",\"route\":\"web/static\",\"version\":1}}",
                sample().release_id()
            )
        );
        let decoded = decode_release(&encode_release(&doc).unwrap()).unwrap();
        assert_eq!(decoded.release, sample().release_id());
        assert_eq!(decoded.entries["img/a.jpg"], "img/a.aaaaaaaaaaaaaaaa.jpg");
    }

    #[test]
    fn current_document_matches_python_layout() {
        let text = String::from_utf8(
            encode_current(&build_current_document(
                &sample(),
                "2026-11-06T00:00:00+00:00",
            ))
            .unwrap(),
        )
        .unwrap();
        let expected = format!(
            "{{\n  \"map_sha256\": \"{}\",\n  \"published_at\": \"2026-11-06T00:00:00+00:00\",\n  \"release\": \"{}\",\n  \"version\": 1\n}}\n",
            sample().content_sha256(),
            sample().release_id()
        );
        assert_eq!(text, expected);
        let decoded = decode_current(text.as_bytes()).unwrap();
        assert_eq!(decoded.release, sample().release_id());
    }

    #[test]
    fn version_mismatch_is_error() {
        let err = decode_current(br#"{"version": 2, "release": "x"}"#).unwrap_err();
        assert!(err.to_string().contains("version 2"));
    }

    #[test]
    fn iso8601_format() {
        assert_eq!(format_iso8601_utc(0), "1970-01-01T00:00:00+00:00");
        assert_eq!(
            format_iso8601_utc(1_793_923_200),
            "2026-11-06T00:00:00+00:00"
        );
        assert_eq!(format_iso8601_utc(951_782_400), "2000-02-29T00:00:00+00:00");
    }
}
