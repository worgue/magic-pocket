//! `versioning = "object_hash"` の route の runtime `static_url(path)`。
//! Python 側 `pocket.static.StaticResolver` と同じ契約。
//!
//! `pocket static publish` が S3 の metadata prefix に置く current (JSON) → release 表
//! (gzip JSON、`path → rel_key`) を読み、論理 path を内容 hash 付きの URL にする。
//! 表に無い path は [`PocketError::StaticPathNotFound`] (hash なしの URL を黙って返さない)。
//! 初回の取得失敗は Err で、呼び出し側が次で再試行する。以降は [`StaticResolver::url`] の
//! 呼び出し時に経過時間 (既定 60 秒) を見て current を HEAD し、ETag が変わったときだけ
//! release 表を 1 本読み直して差し替える。読み直しの失敗では前の版を使い続ける。
//!
//! # Example (axum)
//!
//! ```ignore
//! use magic_pocket_rs::static_url::StaticResolver;
//!
//! // 起動時 (set_envs の後。POCKET_CLOUDFRONT_<NAME>_DOMAIN を読む)
//! let config = magic_pocket_rs::config::load_config("prod")?;
//! let resolver = StaticResolver::load(&config, "web", Some("static")).await?;
//!
//! // handler 内 (必要なら refresh してから解決)
//! let url = resolver.url("img/photo.jpg").await?;
//! // refresh せず同期で解決 (テンプレート関数など await できない場所)
//! let url = resolver.url_cached("img/photo.jpg")?;
//! ```

use std::collections::HashMap;
use std::future::Future;
use std::io::Read;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};

use percent_encoding::{utf8_percent_encode, AsciiSet, NON_ALPHANUMERIC};
use serde::Deserialize;
use tracing::warn;

use crate::config::PocketConfig;
use crate::error::{PocketError, Result};

/// key 名に埋める内容 hash (SHA-256 hex) の長さ (Python の `pocket.static.naming.HASH_LEN`)
pub const HASH_LEN: usize = 16;

/// release 表 / current の version (Python の `pocket.static.release.RELEASE_VERSION`)
pub const RELEASE_VERSION: u64 = 1;

/// refresh の既定間隔
pub const DEFAULT_REFRESH_INTERVAL: Duration = Duration::from_secs(60);

/// URL の path 部分で encode しない文字: RFC 3986 の unreserved (`A-Za-z0-9-._~`) と `/`。
/// Python の `urllib.parse.quote(path, safe="/")` と同じ集合
const URL_PATH_ENCODE_SET: &AsciiSet = &NON_ALPHANUMERIC
    .remove(b'-')
    .remove(b'.')
    .remove(b'_')
    .remove(b'~')
    .remove(b'/');

/// 論理 path に内容 hash を埋めた key 名 (Python の `pocket.static.naming.hashed_name`)。
///
/// `<dir>/<stem>.<hash16>.<ext>`。拡張子が無ければ `<stem>.<hash16>`。先頭の dot だけの
/// 名前 (`.htaccess`) は拡張子ではなく名前として扱い `.htaccess.<hash16>` にする。
/// `archive.tar.gz` は最後の dot で分けて `archive.tar.<hash16>.gz`。
pub fn hashed_name(path: &str, sha256_hex: &str) -> String {
    let digest: String = sha256_hex.chars().take(HASH_LEN).collect();
    let (head, base) = match path.rfind('/') {
        Some(i) => (&path[..=i], &path[i + 1..]),
        None => ("", path),
    };
    let base = match base.rfind('.') {
        Some(dot) if dot > 0 => format!("{}.{}{}", &base[..dot], digest, &base[dot..]),
        _ => format!("{base}.{digest}"),
    };
    format!("{head}{base}")
}

/// URL の path 部分に埋める形に percent-encode する (Python の `encode_url_path`)。
/// `A-Za-z0-9-._~/` 以外をすべて encode する。日本語は UTF-8 の byte 単位
pub fn encode_url_path(path: &str) -> String {
    utf8_percent_encode(path, URL_PATH_ENCODE_SET).to_string()
}

/// `current` の中身 (Python の `build_current_document`)
#[derive(Debug, Deserialize)]
struct CurrentDocument {
    version: u64,
    release: String,
}

/// release 表の中身 (Python の `build_release_document`)
#[derive(Debug, Deserialize)]
struct ReleaseDocument {
    version: u64,
    release: String,
    entries: HashMap<String, String>,
}

fn release_error(message: impl std::fmt::Display) -> PocketError {
    PocketError::StaticRelease(message.to_string())
}

/// current の JSON を読む。version != 1 は Err
fn parse_current(bytes: &[u8]) -> Result<CurrentDocument> {
    let doc: CurrentDocument = serde_json::from_slice(bytes)
        .map_err(|e| release_error(format!("current を読めません: {e}")))?;
    if doc.version != RELEASE_VERSION {
        return Err(release_error(format!(
            "current の version {} は読めません (対応: {}). pocket の版を揃えてください",
            doc.version, RELEASE_VERSION
        )));
    }
    Ok(doc)
}

/// release 表 (gzip JSON) を読む。version != 1 は Err
fn parse_release(bytes: &[u8]) -> Result<ReleaseDocument> {
    let mut json = Vec::new();
    flate2::read::GzDecoder::new(bytes)
        .read_to_end(&mut json)
        .map_err(|e| release_error(format!("release 表を展開できません: {e}")))?;
    let doc: ReleaseDocument = serde_json::from_slice(&json)
        .map_err(|e| release_error(format!("release 表を読めません: {e}")))?;
    if doc.version != RELEASE_VERSION {
        return Err(release_error(format!(
            "release 表の version {} は読めません (対応: {}). pocket の版を揃えてください",
            doc.version, RELEASE_VERSION
        )));
    }
    Ok(doc)
}

/// current / release 表の置き場。本番は [`aws_sdk_s3::Client`]、テストはメモリ実装
pub trait StaticStore: Send + Sync {
    /// object の本文と ETag。存在しなければ Err
    fn get(
        &self,
        bucket: &str,
        key: &str,
    ) -> impl Future<Output = Result<(Vec<u8>, String)>> + Send;

    /// object の ETag だけ取る (HEAD)。存在しなければ Err
    fn head_etag(&self, bucket: &str, key: &str) -> impl Future<Output = Result<String>> + Send;
}

impl StaticStore for aws_sdk_s3::Client {
    async fn get(&self, bucket: &str, key: &str) -> Result<(Vec<u8>, String)> {
        let output = self
            .get_object()
            .bucket(bucket)
            .key(key)
            .send()
            .await
            .map_err(|e| {
                PocketError::S3(format!(
                    "get_object {bucket}/{key}: {}",
                    aws_sdk_s3::error::DisplayErrorContext(&e)
                ))
            })?;
        let etag = output.e_tag().unwrap_or_default().to_string();
        let body = output
            .body
            .collect()
            .await
            .map_err(|e| PocketError::S3(format!("read body {bucket}/{key}: {e}")))?
            .into_bytes()
            .to_vec();
        Ok((body, etag))
    }

    async fn head_etag(&self, bucket: &str, key: &str) -> Result<String> {
        let output = self
            .head_object()
            .bucket(bucket)
            .key(key)
            .send()
            .await
            .map_err(|e| {
                PocketError::S3(format!(
                    "head_object {bucket}/{key}: {}",
                    aws_sdk_s3::error::DisplayErrorContext(&e)
                ))
            })?;
        Ok(output.e_tag().unwrap_or_default().to_string())
    }
}

/// 読み込み済みの release 表 1 版
struct Table {
    release_id: String,
    /// current の ETag (変更検知用)
    etag: String,
    entries: HashMap<String, String>,
}

/// refresh 中フラグ。Drop で解除するので、途中で Err / cancel されても次回 refresh できる
struct RefreshGuard<'a>(&'a AtomicBool);

impl Drop for RefreshGuard<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}

/// object_hash route の URL resolver。本番の S3 client を使う型は [`S3StaticResolver`]
pub struct StaticResolver<S: StaticStore = aws_sdk_s3::Client> {
    bucket: String,
    meta_prefix: String,
    /// `https://<domain><url_prefix>` (末尾 / 付き)
    base_url: String,
    refresh_interval: Duration,
    store: S,
    table: RwLock<Option<Arc<Table>>>,
    checked_at: Mutex<Option<Instant>>,
    refreshing: AtomicBool,
}

/// 本番の S3 client を使う resolver ([`StaticResolver::load`] の戻り値)
pub type S3StaticResolver = StaticResolver<aws_sdk_s3::Client>;

impl S3StaticResolver {
    /// pocket.toml の `[cloudfront.<distribution>]` の object_hash route の resolver。
    ///
    /// `route_ref` は route の `ref`。None なら distribution 内の object_hash route が
    /// 1 つだけのときそれ。配信ドメインは runtime が注入する
    /// `POCKET_CLOUDFRONT_<NAME>_DOMAIN` から取る。初回の current + release 表を
    /// ここで読む (失敗は Err)
    pub async fn load(
        config: &PocketConfig,
        distribution: &str,
        route_ref: Option<&str>,
    ) -> Result<Self> {
        let bucket = config.s3_bucket_name.clone().ok_or_else(|| {
            PocketError::Config("[s3] が無いため static の bucket が分かりません".into())
        })?;
        let candidates: Vec<_> = config
            .static_routes
            .iter()
            .filter(|r| r.cloudfront == distribution)
            .filter(|r| route_ref.is_none_or(|want| r.route_ref.as_deref() == Some(want)))
            .collect();
        if candidates.len() != 1 {
            return Err(PocketError::Config(format!(
                "cloudfront '{distribution}' の object_hash route を特定できません \
                 (候補 {} 件)。route の ref を指定してください",
                candidates.len()
            )));
        }
        let route = candidates[0];
        let env_key = format!("POCKET_CLOUDFRONT_{}_DOMAIN", distribution.to_uppercase());
        let domain = std::env::var(&env_key)
            .ok()
            .filter(|d| !d.is_empty())
            .ok_or_else(|| {
                PocketError::Config(format!(
                    "cloudfront '{distribution}' の配信ドメインが分かりません ({env_key} が未設定。\
                     set_envs の後に呼んでください)"
                ))
            })?;
        let sdk_config = aws_config::defaults(aws_config::BehaviorVersion::latest())
            .region(aws_config::Region::new(config.region.clone()))
            .load()
            .await;
        let resolver = Self::new_with_client(
            bucket,
            route.meta_prefix.clone(),
            format!("https://{domain}{}", route.url_prefix),
            DEFAULT_REFRESH_INTERVAL,
            aws_sdk_s3::Client::new(&sdk_config),
        );
        resolver.ensure_loaded().await?;
        Ok(resolver)
    }
}

impl<S: StaticStore> StaticResolver<S> {
    /// 置き場と client を直接指定して作る (テスト・手動構成用)。表はまだ読まない
    pub fn new_with_client(
        bucket: String,
        meta_prefix: String,
        base_url: String,
        refresh_interval: Duration,
        store: S,
    ) -> Self {
        let base_url = if base_url.ends_with('/') {
            base_url
        } else {
            format!("{base_url}/")
        };
        Self {
            bucket,
            meta_prefix,
            base_url,
            refresh_interval,
            store,
            table: RwLock::new(None),
            checked_at: Mutex::new(None),
            refreshing: AtomicBool::new(false),
        }
    }

    fn current_key(&self) -> String {
        format!("{}current", self.meta_prefix)
    }

    fn release_key(&self, release_id: &str) -> String {
        format!("{}releases/{}.json.gz", self.meta_prefix, release_id)
    }

    fn current_table(&self) -> Option<Arc<Table>> {
        match self.table.read() {
            Ok(guard) => guard.clone(),
            // poison は書き込み中の panic。表自体は壊れないので中身を使う
            Err(poisoned) => poisoned.into_inner().clone(),
        }
    }

    fn swap_table(&self, table: Table) {
        let mut guard = match self.table.write() {
            Ok(g) => g,
            Err(poisoned) => poisoned.into_inner(),
        };
        *guard = Some(Arc::new(table));
    }

    fn mark_checked(&self) {
        let mut guard = match self.checked_at.lock() {
            Ok(g) => g,
            Err(poisoned) => poisoned.into_inner(),
        };
        *guard = Some(Instant::now());
    }

    fn refresh_due(&self) -> bool {
        let guard = match self.checked_at.lock() {
            Ok(g) => g,
            Err(poisoned) => poisoned.into_inner(),
        };
        match *guard {
            Some(at) => at.elapsed() >= self.refresh_interval,
            None => true,
        }
    }

    /// current → release 表を読む
    async fn load_table(&self) -> Result<Table> {
        let (current_bytes, etag) = self.store.get(&self.bucket, &self.current_key()).await?;
        let current = parse_current(&current_bytes)?;
        let (release_bytes, _) = self
            .store
            .get(&self.bucket, &self.release_key(&current.release))
            .await?;
        let release = parse_release(&release_bytes)?;
        if release.release != current.release {
            return Err(release_error(format!(
                "release 表 {} の中身の id が違います: {}",
                current.release, release.release
            )));
        }
        Ok(Table {
            release_id: current.release,
            etag,
            entries: release.entries,
        })
    }

    /// 表が未読なら読む (初回の失敗は Err)
    async fn ensure_loaded(&self) -> Result<Arc<Table>> {
        if let Some(table) = self.current_table() {
            return Ok(table);
        }
        let table = self.load_table().await?;
        self.swap_table(table);
        self.mark_checked();
        self.current_table()
            .ok_or_else(|| release_error("release 表を保持できませんでした"))
    }

    /// 期限が来ていれば current を HEAD し、ETag が変わっていたら表を差し替える。
    /// 失敗は warn で前の版を使い続ける。同時に来た呼び出しは 1 つだけが S3 を見る
    async fn refresh_if_due(&self) {
        if !self.refresh_due() {
            return;
        }
        if self
            .refreshing
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_err()
        {
            return;
        }
        let _guard = RefreshGuard(&self.refreshing);
        self.mark_checked();
        let Some(current) = self.current_table() else {
            return;
        };
        let result = async {
            let etag = self
                .store
                .head_etag(&self.bucket, &self.current_key())
                .await?;
            if etag == current.etag {
                return Ok(());
            }
            let table = self.load_table().await?;
            self.swap_table(table);
            Ok::<(), PocketError>(())
        }
        .await;
        if let Err(e) = result {
            warn!(
                "static の release 表を読み直せません (前の版 {} を使います): {e}",
                current.release_id
            );
        }
    }

    fn resolve(&self, table: &Table, path: &str) -> Result<String> {
        match table.entries.get(path) {
            Some(rel_key) => Ok(format!("{}{}", self.base_url, encode_url_path(rel_key))),
            None => Err(PocketError::StaticPathNotFound {
                path: path.to_string(),
                release: table.release_id.clone(),
            }),
        }
    }

    /// 論理 path を URL にする。期限が来ていれば先に release 表を読み直す
    pub async fn url(&self, path: &str) -> Result<String> {
        let table = self.ensure_loaded().await?;
        self.refresh_if_due().await;
        let table = self.current_table().unwrap_or(table);
        self.resolve(&table, path)
    }

    /// 論理 path を URL にする (refresh せず、読み込み済みの表だけで解決する)。
    /// 表が未読なら Err
    pub fn url_cached(&self, path: &str) -> Result<String> {
        let table = self
            .current_table()
            .ok_or_else(|| release_error("release 表がまだ読み込まれていません"))?;
        self.resolve(&table, path)
    }

    /// 読み込み済みの release id (未読なら None)
    pub fn release_id(&self) -> Option<String> {
        self.current_table().map(|t| t.release_id.clone())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::sync::atomic::AtomicUsize;

    #[derive(Deserialize)]
    struct Vectors {
        cases: Vec<VectorCase>,
    }

    #[derive(Deserialize)]
    struct VectorCase {
        path: String,
        sha256: String,
        hashed_name: String,
        encoded_hashed_name: String,
        encoded_path: String,
    }

    #[test]
    fn test_naming_matches_python_vectors() {
        let raw = include_str!("../../../tests/data/static_url_vectors.json");
        let vectors: Vectors = serde_json::from_str(raw).expect("vector JSON");
        assert!(!vectors.cases.is_empty());
        for case in vectors.cases {
            assert_eq!(
                hashed_name(&case.path, &case.sha256),
                case.hashed_name,
                "{}",
                case.path
            );
            assert_eq!(
                encode_url_path(&case.hashed_name),
                case.encoded_hashed_name,
                "{}",
                case.path
            );
            assert_eq!(
                encode_url_path(&case.path),
                case.encoded_path,
                "{}",
                case.path
            );
        }
    }

    fn gzip(json: &str) -> Vec<u8> {
        let mut enc = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
        enc.write_all(json.as_bytes()).expect("gzip write");
        enc.finish().expect("gzip finish")
    }

    /// メモリ上の置き場。objects は (key → (body, etag))。get / head の回数を数える
    struct MemoryStore {
        objects: Mutex<HashMap<String, (Vec<u8>, String)>>,
        fail_reads: AtomicBool,
        heads: AtomicUsize,
    }

    impl MemoryStore {
        fn new() -> Self {
            Self {
                objects: Mutex::new(HashMap::new()),
                fail_reads: AtomicBool::new(false),
                heads: AtomicUsize::new(0),
            }
        }

        fn put(&self, key: &str, body: Vec<u8>, etag: &str) {
            self.objects
                .lock()
                .expect("lock")
                .insert(key.to_string(), (body, etag.to_string()));
        }

        fn publish(&self, release_id: &str, entries: &[(&str, &str)], etag: &str) {
            let entries_json: Vec<String> = entries
                .iter()
                .map(|(p, k)| format!("{:?}:{:?}", p, k))
                .collect();
            let release = format!(
                r#"{{"version":1,"release":"{release_id}","entries":{{{}}}}}"#,
                entries_json.join(",")
            );
            self.put(
                &format!(".pocket/static/web/static/releases/{release_id}.json.gz"),
                gzip(&release),
                "\"rel\"",
            );
            self.put(
                ".pocket/static/web/static/current",
                format!(r#"{{"version":1,"release":"{release_id}"}}"#).into_bytes(),
                etag,
            );
        }
    }

    impl StaticStore for MemoryStore {
        async fn get(&self, _bucket: &str, key: &str) -> Result<(Vec<u8>, String)> {
            if self.fail_reads.load(Ordering::Acquire) {
                return Err(PocketError::S3("injected failure".into()));
            }
            self.objects
                .lock()
                .expect("lock")
                .get(key)
                .cloned()
                .ok_or_else(|| PocketError::S3(format!("no such key {key}")))
        }

        async fn head_etag(&self, _bucket: &str, key: &str) -> Result<String> {
            self.heads.fetch_add(1, Ordering::AcqRel);
            if self.fail_reads.load(Ordering::Acquire) {
                return Err(PocketError::S3("injected failure".into()));
            }
            self.objects
                .lock()
                .expect("lock")
                .get(key)
                .map(|(_, etag)| etag.clone())
                .ok_or_else(|| PocketError::S3(format!("no such key {key}")))
        }
    }

    fn resolver(store: MemoryStore, interval: Duration) -> StaticResolver<MemoryStore> {
        StaticResolver::new_with_client(
            "bucket".into(),
            ".pocket/static/web/static/".into(),
            "https://cdn.example.com/static/".into(),
            interval,
            store,
        )
    }

    fn block<T>(f: impl Future<Output = T>) -> T {
        tokio::runtime::Runtime::new().expect("runtime").block_on(f)
    }

    #[test]
    fn test_url_resolves_and_unknown_path_is_error() {
        let store = MemoryStore::new();
        store.publish(
            "rel1",
            &[
                ("img/photo.jpg", "img/photo.ca978112ca1bbdca.jpg"),
                ("img/写真 1.jpg", "img/写真 1.3e23e8160039594a.jpg"),
            ],
            "\"etag1\"",
        );
        let r = resolver(store, Duration::from_secs(60));
        assert!(r.url_cached("img/photo.jpg").is_err(), "未読なら Err");
        let url = block(r.url("img/photo.jpg")).expect("url");
        assert_eq!(
            url,
            "https://cdn.example.com/static/img/photo.ca978112ca1bbdca.jpg"
        );
        assert_eq!(
            r.url_cached("img/写真 1.jpg").expect("cached"),
            "https://cdn.example.com/static/img/%E5%86%99%E7%9C%9F%201.3e23e8160039594a.jpg"
        );
        assert_eq!(r.release_id().as_deref(), Some("rel1"));
        match block(r.url("missing.png")) {
            Err(PocketError::StaticPathNotFound { path, release }) => {
                assert_eq!(path, "missing.png");
                assert_eq!(release, "rel1");
            }
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn test_refresh_swaps_table_when_etag_changes() {
        let store = MemoryStore::new();
        store.publish("rel1", &[("a.txt", "a.1111111111111111.txt")], "\"etag1\"");
        let r = resolver(store, Duration::ZERO);
        block(r.url("a.txt")).expect("first load");
        // 同じ ETag のままなら HEAD だけで表は変わらない
        block(r.url("a.txt")).expect("same");
        assert_eq!(r.release_id().as_deref(), Some("rel1"));
        assert!(r.store.heads.load(Ordering::Acquire) >= 1);
        // 新しい release に切り替わる
        r.store
            .publish("rel2", &[("a.txt", "a.2222222222222222.txt")], "\"etag2\"");
        let url = block(r.url("a.txt")).expect("after switch");
        assert_eq!(url, "https://cdn.example.com/static/a.2222222222222222.txt");
        assert_eq!(r.release_id().as_deref(), Some("rel2"));
    }

    #[test]
    fn test_refresh_failure_keeps_previous_table() {
        let store = MemoryStore::new();
        store.publish("rel1", &[("a.txt", "a.1111111111111111.txt")], "\"etag1\"");
        let r = resolver(store, Duration::ZERO);
        block(r.url("a.txt")).expect("first load");
        r.store.fail_reads.store(true, Ordering::Release);
        let url = block(r.url("a.txt")).expect("still resolves");
        assert_eq!(url, "https://cdn.example.com/static/a.1111111111111111.txt");
        assert_eq!(r.release_id().as_deref(), Some("rel1"));
        // 失敗後も refresh フラグは解除されている (次の refresh が走る)
        assert!(!r.refreshing.load(Ordering::Acquire));
        r.store.fail_reads.store(false, Ordering::Release);
        r.store
            .publish("rel2", &[("a.txt", "a.2222222222222222.txt")], "\"etag2\"");
        let url = block(r.url("a.txt")).expect("recovered");
        assert_eq!(url, "https://cdn.example.com/static/a.2222222222222222.txt");
    }

    #[test]
    fn test_initial_load_failure_is_error_and_retryable() {
        let store = MemoryStore::new();
        let r = resolver(store, Duration::from_secs(60));
        assert!(block(r.url("a.txt")).is_err(), "current が無ければ Err");
        r.store
            .publish("rel1", &[("a.txt", "a.1111111111111111.txt")], "\"etag1\"");
        block(r.url("a.txt")).expect("retry succeeds");
    }

    #[test]
    fn test_release_version_mismatch_is_error() {
        let store = MemoryStore::new();
        store.put(
            ".pocket/static/web/static/current",
            br#"{"version":2,"release":"x"}"#.to_vec(),
            "\"e\"",
        );
        let r = resolver(store, Duration::from_secs(60));
        match block(r.url("a.txt")) {
            Err(PocketError::StaticRelease(msg)) => assert!(msg.contains("version 2")),
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn test_release_id_mismatch_is_error() {
        let store = MemoryStore::new();
        store.put(
            ".pocket/static/web/static/releases/rel1.json.gz",
            gzip(r#"{"version":1,"release":"other","entries":{}}"#),
            "\"r\"",
        );
        store.put(
            ".pocket/static/web/static/current",
            br#"{"version":1,"release":"rel1"}"#.to_vec(),
            "\"e\"",
        );
        let r = resolver(store, Duration::from_secs(60));
        assert!(matches!(
            block(r.url("a.txt")),
            Err(PocketError::StaticRelease(_))
        ));
    }
}
