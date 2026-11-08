//! `versioning = "object_hash"` の route の publish エンジン (旧 Python 版
//! `StaticPublisher` の契約を引き継ぐ)。
//!
//! - 同じ key に別の内容は載らない: PutObject は `If-None-Match: *` と `ChecksumSHA256`
//!   を付け、412 なら既存の checksum を map の hash と照合する
//! - 変更なしの再 publish は upload 0 件: S3 の一覧に無い key だけ上げる
//! - 途中で止まっても再実行で続きから進む: 上げ終わった key は次回の一覧で除かれる
//! - hash 後にローカルが変わっても壊れない: upload は checksum で S3 が拒否し、adopt は
//!   CopySourceIfMatch で元を固定する。失敗したら current を更新しない
//!
//! メモリ: hash は 1MiB chunk のストリーミング、PUT は `ByteStream::from_path` でファイルから
//! 流す (本文を保持しない)。hash・転送とも並列数は `jobs`

use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{anyhow, bail, Context, Result};
use aws_sdk_s3::error::{ProvideErrorMetadata, SdkError};
use aws_sdk_s3::primitives::ByteStream;
use aws_sdk_s3::types::{
    ChecksumAlgorithm, ChecksumMode, ChecksumType, Delete, MetadataDirective, ObjectIdentifier,
};
use aws_sdk_s3::Client;
use base64::Engine;
use magic_pocket_rs::config::StaticRouteConfig;
use magic_pocket_rs::static_url::encode_url_path;
use md5::Md5;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use tokio::sync::Semaphore;
use tokio::task::JoinSet;

use crate::mapfile::{MapEntry, StaticMap};
use crate::release::{
    build_current_document, build_release_document, current_key, decode_current, decode_release,
    encode_current, encode_release, now_iso8601_utc, release_key, CurrentDocument,
};
use crate::sources::{enumerate_sources, LocalFile};

/// 単一 PUT の上限。第 1 段は multipart を使わない
const SINGLE_PUT_LIMIT: u64 = 5 * 1024 * 1024 * 1024;
const CHUNK: usize = 1024 * 1024;
const DELETE_BATCH: usize = 1000;

#[derive(Debug, Default)]
pub struct PublishResult {
    pub uploaded: usize,
    pub copied: usize,
    pub skipped: usize,
    pub verified: usize,
}

#[derive(Debug, Default, Clone, Copy)]
pub struct PublishOptions {
    pub hash_generate: bool,
    pub allow_dirty: bool,
    pub adopt: bool,
    pub verify_existing: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Outcome {
    Uploaded,
    Copied,
    Verified,
}

pub fn digest_file(file: &Path) -> Result<(String, String)> {
    let mut f =
        std::fs::File::open(file).with_context(|| format!("{} を開けません", file.display()))?;
    let mut sha = Sha256::new();
    let mut md5 = Md5::new();
    let mut buf = vec![0u8; CHUNK];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        sha.update(&buf[..n]);
        md5.update(&buf[..n]);
    }
    Ok((hex::encode(sha.finalize()), hex::encode(md5.finalize())))
}

fn b64_sha256(sha256_hex: &str) -> Result<String> {
    let raw = hex::decode(sha256_hex)?;
    Ok(base64::engine::general_purpose::STANDARD.encode(raw))
}

fn is_plain_md5_etag(etag: Option<&str>) -> bool {
    matches!(etag, Some(e) if !e.is_empty() && !e.contains('-'))
}

fn strip_quotes(etag: &str) -> &str {
    etag.trim_matches('"')
}

fn content_type_of(path: &str) -> String {
    mime_guess::from_path(path)
        .first_raw()
        .unwrap_or("application/octet-stream")
        .to_string()
}

type PutObjectSdkError = SdkError<aws_sdk_s3::operation::put_object::PutObjectError, HttpResponse>;
type HttpResponse = aws_sdk_s3::config::http::HttpResponse;

fn http_status<E>(e: &SdkError<E, HttpResponse>) -> Option<u16> {
    match e {
        SdkError::ServiceError(se) => Some(se.raw().status().as_u16()),
        _ => None,
    }
}

fn is_precondition_failed<E: ProvideErrorMetadata>(e: &SdkError<E, HttpResponse>) -> bool {
    e.code() == Some("PreconditionFailed") || http_status(e) == Some(412)
}

fn is_not_found<E: ProvideErrorMetadata>(e: &SdkError<E, HttpResponse>) -> bool {
    matches!(e.code(), Some("404" | "NoSuchKey" | "NotFound")) || http_status(e) == Some(404)
}

/// 1 件の PutObject / CopyObject を一過性のエラーで諦めるまでの試行回数と、待ち時間の
/// 初期値 (1 秒, 2 秒, ...)。SDK 自身の retry の外側で、proxy の CONNECT 失敗など接続層の
/// 失敗を拾う (数万件の publish を 1 件の瞬断で止めないため)。Python 版と同じ値
const TRANSFER_ATTEMPTS: u32 = 3;
const TRANSFER_RETRY_BASE_DELAY: Duration = Duration::from_secs(1);

/// 接続層の失敗・timeout・5xx・SlowDown なら true。4xx (412 / 403 等) は false
fn is_transient<E: ProvideErrorMetadata>(e: &SdkError<E, HttpResponse>) -> bool {
    match e {
        SdkError::DispatchFailure(_) | SdkError::TimeoutError(_) | SdkError::ResponseError(_) => {
            true
        }
        SdkError::ServiceError(se) => {
            se.raw().status().as_u16() >= 500
                || matches!(
                    e.code(),
                    Some("SlowDown" | "RequestTimeout" | "InternalError" | "ServiceUnavailable")
                )
        }
        _ => false,
    }
}

/// `op()` を一過性のエラーのときだけ最大 `attempts` 回まで繰り返す (1 件単位)。
/// それ以外のエラーと、最後の試行の失敗はそのまま返す (publish 全体が止まる。
/// 再実行で続きから進む)
async fn retry_transient<T, E, Fut>(
    what: &str,
    attempts: u32,
    base_delay: Duration,
    mut op: impl FnMut() -> Fut,
) -> std::result::Result<T, SdkError<E, HttpResponse>>
where
    E: ProvideErrorMetadata + std::fmt::Debug,
    Fut: Future<Output = std::result::Result<T, SdkError<E, HttpResponse>>>,
{
    let mut attempt = 1;
    loop {
        match op().await {
            Ok(v) => return Ok(v),
            Err(e) if attempt < attempts && is_transient(&e) => {
                let delay = base_delay * 2u32.pow(attempt - 1);
                warn(&format!(
                    "{what}: 一過性のエラーのため {} 秒後に再試行します ({attempt}/{attempts}): {e}",
                    delay.as_secs_f64()
                ));
                tokio::time::sleep(delay).await;
                attempt += 1;
            }
            Err(e) => return Err(e),
        }
    }
}

fn s3_err<E: std::fmt::Debug>(e: SdkError<E, HttpResponse>, what: &str) -> anyhow::Error {
    let detail = match &e {
        SdkError::ServiceError(se) => format!("{:?}", se.err()),
        other => format!("{other:?}"),
    };
    anyhow!("{what}: {detail}")
}

pub struct StaticPublisher {
    pub route: StaticRouteConfig,
    pub bucket: String,
    pub base_dir: PathBuf,
    pub jobs: usize,
    pub s3: Client,
}

impl StaticPublisher {
    pub fn new(
        route: StaticRouteConfig,
        bucket: String,
        base_dir: PathBuf,
        jobs: usize,
        s3: Client,
    ) -> Self {
        Self {
            route,
            bucket,
            base_dir,
            jobs: jobs.max(1),
            s3,
        }
    }

    // --- 設定から導く名前 ---

    pub fn route_label(&self) -> String {
        format!("{}/{}", self.route.cloudfront, self.route.route_name)
    }

    pub fn map_file(&self) -> PathBuf {
        self.base_dir.join(&self.route.map_path)
    }

    pub fn key_prefix(&self) -> &str {
        &self.route.key_prefix
    }

    pub fn meta_prefix(&self) -> &str {
        &self.route.meta_prefix
    }

    pub fn cache_control(&self) -> String {
        format!(
            "public, max-age={}, immutable",
            self.route.versioned_max_age
        )
    }

    // --- ローカルの列挙と hash ---

    pub fn enumerate_sources(&self) -> Result<Vec<LocalFile>> {
        enumerate_sources(&self.base_dir, &self.route.sources)
    }

    pub async fn hash_files(&self, files: &mut [LocalFile]) -> Result<()> {
        for f in files.iter() {
            if f.size > SINGLE_PUT_LIMIT {
                bail!(
                    "{} は 5GB を超えています (第 1 段は単一 PUT のみ)",
                    f.file.display()
                );
            }
        }
        let semaphore = Arc::new(Semaphore::new(self.jobs));
        let mut set: JoinSet<Result<(usize, String, String)>> = JoinSet::new();
        for (i, f) in files.iter().enumerate() {
            let permit = semaphore.clone().acquire_owned().await?;
            let path = f.file.clone();
            set.spawn_blocking(move || {
                let _permit = permit;
                let (sha, md5) = digest_file(&path)?;
                Ok((i, sha, md5))
            });
        }
        while let Some(joined) = set.join_next().await {
            let (i, sha, md5) = joined??;
            files[i].sha256 = sha;
            files[i].md5 = md5;
        }
        Ok(())
    }

    pub fn build_map(&self, files: &[LocalFile]) -> Result<StaticMap> {
        let mut static_map = StaticMap::new(&self.route_label());
        for f in files {
            static_map.add(f.entry())?;
        }
        check_rel_key_collisions(&static_map)?;
        Ok(static_map)
    }

    // --- map ファイル ---

    pub fn write_map(&self, static_map: &StaticMap) -> Result<PathBuf> {
        let path = self.map_file();
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(&path, static_map.dumps())
            .with_context(|| format!("{} を書けません", path.display()))?;
        Ok(path)
    }

    pub fn read_map(&self) -> Result<StaticMap> {
        let path = self.map_file();
        if !path.is_file() {
            bail!(
                "map ファイルがありません: {}\n  初回は `pocket-rs static publish --hash-generate` か `pocket-rs static hash generate` で作ってください",
                path.display()
            );
        }
        let text = std::fs::read_to_string(&path)?;
        let static_map = StaticMap::loads(&text).map_err(|e| anyhow!("{}: {e}", path.display()))?;
        if static_map.route != self.route_label() {
            bail!(
                "{} は route {} の map です (この route は {})",
                path.display(),
                static_map.route,
                self.route_label()
            );
        }
        check_rel_key_collisions(&static_map)?;
        Ok(static_map)
    }

    /// map が git で未 commit / 作業ツリーと差があれば理由を返す。git 外は None
    pub fn map_dirty_reason(&self) -> Option<String> {
        let output = match Command::new("git")
            .args(["status", "--porcelain", "--"])
            .arg(self.map_file())
            .current_dir(&self.base_dir)
            .output()
        {
            Ok(o) => o,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                warn("git が無いため map の commit 状態を確認できません");
                return None;
            }
            Err(e) => {
                warn(&format!(
                    "git を実行できないため map の commit 状態を確認できません: {e}"
                ));
                return None;
            }
        };
        if !output.status.success() {
            warn(&format!(
                "git repo ではないため map の commit 状態を確認できません: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            ));
            return None;
        }
        let status = String::from_utf8_lossy(&output.stdout).trim().to_string();
        if status.is_empty() {
            return None;
        }
        if status.starts_with("??") {
            return Some("map が git に追加されていません (untracked)".to_string());
        }
        let code = status.split_whitespace().next().unwrap_or("");
        Some(format!("map が commit 済みの内容と違います ({code})"))
    }

    // --- S3 ---

    /// route の key prefix 配下の {key: ETag}
    pub async fn list_existing(&self) -> Result<HashMap<String, String>> {
        self.list_prefix(self.key_prefix()).await
    }

    async fn list_prefix(&self, prefix: &str) -> Result<HashMap<String, String>> {
        let mut existing = HashMap::new();
        let mut pages = self
            .s3
            .list_objects_v2()
            .bucket(&self.bucket)
            .prefix(prefix)
            .into_paginator()
            .send();
        while let Some(page) = pages.next().await {
            let page = page.map_err(|e| s3_err(e, "ListObjectsV2"))?;
            for obj in page.contents() {
                if let Some(key) = obj.key() {
                    existing.insert(key.to_string(), obj.e_tag().unwrap_or("").to_string());
                }
            }
        }
        Ok(existing)
    }

    /// 既存 object の (ChecksumSHA256 base64 (FULL_OBJECT のみ), ETag)
    async fn head_checksum(&self, key: &str) -> Result<(Option<String>, Option<String>)> {
        let head = self
            .s3
            .head_object()
            .bucket(&self.bucket)
            .key(key)
            .checksum_mode(ChecksumMode::Enabled)
            .send()
            .await
            .map_err(|e| s3_err(e, &format!("HeadObject {key}")))?;
        let mut checksum = head.checksum_sha256().map(|s| s.to_string());
        if checksum.is_some() {
            if let Some(t) = head.checksum_type() {
                if *t != ChecksumType::FullObject {
                    checksum = None;
                }
            }
        }
        Ok((checksum, head.e_tag().map(|s| s.to_string())))
    }

    /// S3 にある key の内容が map の hash と一致することを確かめる
    pub async fn verify_existing(
        &self,
        key: &str,
        entry: &MapEntry,
        md5: Option<&str>,
    ) -> Result<()> {
        let (checksum, etag) = self.head_checksum(key).await?;
        if let Some(checksum) = checksum {
            if checksum != b64_sha256(&entry.sha256)? {
                bail!(
                    "s3://{}/{} の内容が map の hash と違います (path {:?})。内容を変えたなら名前も変えてください",
                    self.bucket,
                    key,
                    entry.path
                );
            }
            return Ok(());
        }
        if let Some(md5) = md5 {
            if is_plain_md5_etag(etag.as_deref()) {
                if strip_quotes(etag.as_deref().unwrap_or("")) != md5 {
                    bail!(
                        "s3://{}/{} の内容がローカルと違います (path {:?})。内容を変えたなら名前も変えてください",
                        self.bucket,
                        key,
                        entry.path
                    );
                }
                return Ok(());
            }
        }
        bail!(
            "s3://{}/{} の内容を確かめられません (SHA-256 checksum が無く、ETag も MD5 ではありません)。path {:?}",
            self.bucket,
            key,
            entry.path
        )
    }

    async fn put(
        &self,
        key: &str,
        local: &LocalFile,
    ) -> std::result::Result<(), Box<PutObjectSdkError>> {
        // SdkError は大きいので Box で返す (clippy::result_large_err)
        let checksum =
            b64_sha256(&local.sha256).map_err(|e| Box::new(SdkError::construction_failure(e)))?;
        // body は send で消費されるので試行ごとに開き直す
        retry_transient(
            &format!("PutObject {key}"),
            TRANSFER_ATTEMPTS,
            TRANSFER_RETRY_BASE_DELAY,
            || async {
                let body = ByteStream::from_path(&local.file)
                    .await
                    .map_err(SdkError::construction_failure)?;
                self.s3
                    .put_object()
                    .bucket(&self.bucket)
                    .key(key)
                    .body(body)
                    .if_none_match("*")
                    .checksum_sha256(&checksum)
                    .content_type(content_type_of(&local.path))
                    .cache_control(self.cache_control())
                    .send()
                    .await
            },
        )
        .await
        .map_err(Box::new)?;
        Ok(())
    }

    /// 1 件 upload する
    async fn upload_one(&self, key: &str, local: &LocalFile) -> Result<Outcome> {
        match self.put(key, local).await {
            Ok(()) => return Ok(Outcome::Uploaded),
            Err(e) if is_precondition_failed(&e) => {}
            Err(e) => return Err(s3_err(*e, &format!("PutObject {key}"))),
        }
        // 並行する publish が先に置いた (内容 hash が同じなら同じ内容のはず)。照合する
        self.verify_existing(key, &local.entry(), Some(&local.md5))
            .await?;
        Ok(Outcome::Verified)
    }

    /// 平置き key から server-side copy する。できなければ None
    async fn copy_from_flat(
        &self,
        key: &str,
        local: &LocalFile,
        flat_etag: &str,
    ) -> Result<Option<Outcome>> {
        if !is_plain_md5_etag(Some(flat_etag)) || strip_quotes(flat_etag) != local.md5 {
            return Ok(None);
        }
        let flat_key = format!("{}{}", self.key_prefix(), local.path);
        let copy_source = format!("{}/{}", self.bucket, encode_url_path(&flat_key));
        let result = retry_transient(
            &format!("CopyObject {key}"),
            TRANSFER_ATTEMPTS,
            TRANSFER_RETRY_BASE_DELAY,
            || {
                self.s3
                    .copy_object()
                    .bucket(&self.bucket)
                    .key(key)
                    .copy_source(&copy_source)
                    .copy_source_if_match(flat_etag)
                    .metadata_directive(MetadataDirective::Replace)
                    .content_type(content_type_of(&local.path))
                    .cache_control(self.cache_control())
                    .checksum_algorithm(ChecksumAlgorithm::Sha256)
                    .send()
            },
        )
        .await;
        match result {
            Ok(_) => {}
            Err(e) if is_precondition_failed(&e) => return Ok(None),
            Err(e) => return Err(s3_err(e, &format!("CopyObject {key}"))),
        }
        self.verify_existing(key, &local.entry(), Some(&local.md5))
            .await?;
        Ok(Some(Outcome::Copied))
    }

    // --- publish ---

    pub async fn publish(self: Arc<Self>, opts: PublishOptions) -> Result<PublishResult> {
        // 開始時点の current。最後の If-Match に同じ値を使い、途中で別の publish が
        // 切り替えていたら競合として止める
        let current_etag = self.current_etag().await?;

        let mut files = self.enumerate_sources()?;
        eprintln!(
            "[{}] {} ファイルを hash します",
            self.route_label(),
            files.len()
        );
        self.hash_files(&mut files).await?;
        let local_map = self.build_map(&files)?;
        let by_path: HashMap<String, LocalFile> =
            files.into_iter().map(|f| (f.path.clone(), f)).collect();

        let static_map = if opts.hash_generate {
            let path = self.write_map(&local_map)?;
            eprintln!(
                "map を書きました: {} (release {})",
                path.display(),
                local_map.release_id()
            );
            local_map
        } else {
            self.read_committed_map(opts.allow_dirty)?
        };

        let existing = self.list_existing().await?;
        let mut result = PublishResult::default();
        let missing_local = self.check_against_map(&static_map, &by_path, &existing)?;

        // P (名前のまま) は key に hash が無いので、既存 key の内容を必ず照合する
        for entry in &missing_local {
            if !entry.hashed || opts.verify_existing {
                self.verify_existing(
                    &format!("{}{}", self.key_prefix(), entry.rel_key()),
                    entry,
                    None,
                )
                .await?;
                result.verified += 1;
            } else {
                result.skipped += 1;
            }
        }

        let mut targets: Vec<(String, LocalFile)> = Vec::new();
        let mut paths: Vec<&String> = by_path.keys().collect();
        paths.sort();
        for path in paths {
            let local = &by_path[path];
            let key = format!("{}{}", self.key_prefix(), local.entry().rel_key());
            if existing.contains_key(&key) {
                if !local.hashed || opts.verify_existing {
                    self.verify_existing(&key, &local.entry(), Some(&local.md5))
                        .await?;
                    result.verified += 1;
                } else {
                    result.skipped += 1;
                }
                continue;
            }
            targets.push((key, local.clone()));
        }

        eprintln!(
            "[{}] upload {} 件 (skip {}, 照合 {})",
            self.route_label(),
            targets.len(),
            result.skipped,
            result.verified
        );
        self.clone()
            .transfer(targets, Arc::new(existing), opts.adopt, &mut result)
            .await?;
        self.write_release(&static_map, current_etag.as_deref())
            .await?;
        eprintln!(
            "[{}] release {} を公開しました (upload {}, copy {}, skip {}, 照合 {})",
            self.route_label(),
            static_map.release_id(),
            result.uploaded,
            result.copied,
            result.skipped,
            result.verified
        );
        Ok(result)
    }

    fn read_committed_map(&self, allow_dirty: bool) -> Result<StaticMap> {
        let static_map = self.read_map()?;
        if let Some(reason) = self.map_dirty_reason() {
            if !allow_dirty {
                bail!(
                    "{}: {reason}。commit してから publish するか、--allow-dirty を付けてください",
                    self.map_file().display()
                );
            }
            warn(&format!(
                "{}: {reason} (--allow-dirty)",
                self.map_file().display()
            ));
        }
        Ok(static_map)
    }

    /// ローカルと map を照合する。戻り値は map にだけある (S3 に key がある) entry。
    ///
    /// ローカルにあるものは map と一致していること。map にだけあるものは S3 に
    /// key があればよい (2 回目以降の publish は全ファイルが無くても通る)
    fn check_against_map(
        &self,
        static_map: &StaticMap,
        by_path: &HashMap<String, LocalFile>,
        existing: &HashMap<String, String>,
    ) -> Result<Vec<MapEntry>> {
        let mut errors: Vec<String> = Vec::new();
        let mut missing_local: Vec<MapEntry> = Vec::new();
        let mut local_paths: Vec<&String> = by_path.keys().collect();
        local_paths.sort();
        for path in local_paths {
            let local = &by_path[path];
            match static_map.entries.get(path) {
                None => errors.push(format!("map に無いファイル: {path}")),
                Some(entry) if *entry != local.entry() => errors.push(format!(
                    "map と違う: {path} (map: {} / local: {})",
                    describe(entry),
                    describe(&local.entry())
                )),
                Some(_) => {}
            }
        }
        for (path, entry) in &static_map.entries {
            if by_path.contains_key(path) {
                continue;
            }
            let key = format!("{}{}", self.key_prefix(), entry.rel_key());
            if !existing.contains_key(&key) {
                errors.push(format!(
                    "map にあるがローカルにも S3 にも無い: {path} ({key})"
                ));
            } else {
                missing_local.push(entry.clone());
            }
        }
        if !errors.is_empty() {
            bail!(
                "ローカルと map が一致しません ({} 件):\n  {}",
                errors.len(),
                errors
                    .iter()
                    .take(50)
                    .cloned()
                    .collect::<Vec<_>>()
                    .join("\n  ")
            );
        }
        Ok(missing_local)
    }

    async fn transfer(
        self: Arc<Self>,
        targets: Vec<(String, LocalFile)>,
        existing: Arc<HashMap<String, String>>,
        adopt: bool,
        result: &mut PublishResult,
    ) -> Result<()> {
        let total = targets.len();
        let semaphore = Arc::new(Semaphore::new(self.jobs));
        let mut set: JoinSet<Result<Outcome>> = JoinSet::new();
        let mut done = 0usize;
        let mut pending = targets.into_iter();
        // 先に jobs 件まで投入し、以降は 1 件終わるごとに 1 件足す (順序は問わない)
        loop {
            while set.len() < self.jobs {
                let Some((key, local)) = pending.next() else {
                    break;
                };
                let permit = semaphore.clone().acquire_owned().await?;
                let me = self.clone();
                let existing = existing.clone();
                set.spawn(async move {
                    let _permit = permit;
                    if adopt {
                        let flat_key = format!("{}{}", me.key_prefix(), local.path);
                        if let Some(flat_etag) = existing.get(&flat_key) {
                            if let Some(outcome) =
                                me.copy_from_flat(&key, &local, flat_etag).await?
                            {
                                return Ok(outcome);
                            }
                        }
                    }
                    me.upload_one(&key, &local).await
                });
            }
            let Some(joined) = set.join_next().await else {
                break;
            };
            match joined?? {
                Outcome::Uploaded => result.uploaded += 1,
                Outcome::Copied => result.copied += 1,
                Outcome::Verified => result.verified += 1,
            }
            done += 1;
            if done.is_multiple_of(500) || done == total {
                eprintln!("  {done} / {total}");
            }
        }
        Ok(())
    }

    async fn current_etag(&self) -> Result<Option<String>> {
        match self
            .s3
            .head_object()
            .bucket(&self.bucket)
            .key(current_key(self.meta_prefix()))
            .send()
            .await
        {
            Ok(head) => Ok(head.e_tag().map(|s| s.to_string())),
            Err(e) if is_not_found(&e) => Ok(None),
            Err(e) => Err(s3_err(e, "HeadObject current")),
        }
    }

    async fn write_release(
        &self,
        static_map: &StaticMap,
        current_etag: Option<&str>,
    ) -> Result<()> {
        let rel_key = release_key(self.meta_prefix(), &static_map.release_id());
        let body = encode_release(&build_release_document(static_map))?;
        match self
            .s3
            .put_object()
            .bucket(&self.bucket)
            .key(&rel_key)
            .body(ByteStream::from(body))
            .if_none_match("*")
            .content_type("application/json")
            .content_encoding("gzip")
            .send()
            .await
        {
            Ok(_) => {}
            Err(e) if is_precondition_failed(&e) => {}
            Err(e) => return Err(s3_err(e, &format!("PutObject {rel_key}"))),
        }
        let published_at = now_iso8601_utc();
        let body = encode_current(&build_current_document(static_map, &published_at))?;
        let mut req = self
            .s3
            .put_object()
            .bucket(&self.bucket)
            .key(current_key(self.meta_prefix()))
            .body(ByteStream::from(body))
            .content_type("application/json");
        req = match current_etag {
            None => req.if_none_match("*"),
            Some(etag) => req.if_match(etag),
        };
        match req.send().await {
            Ok(_) => Ok(()),
            Err(e) if is_precondition_failed(&e) => {
                bail!("別の publish が同じ route の current を更新しました。内容を確かめてから再実行してください")
            }
            Err(e) => Err(s3_err(e, "PutObject current")),
        }
    }

    // --- status / purge ---

    pub async fn current(&self) -> Result<Option<CurrentDocument>> {
        match self
            .s3
            .get_object()
            .bucket(&self.bucket)
            .key(current_key(self.meta_prefix()))
            .send()
            .await
        {
            Ok(res) => {
                let data = res.body.collect().await?.into_bytes();
                Ok(Some(decode_current(&data)?))
            }
            Err(e) if is_not_found(&e) => Ok(None),
            Err(e) => Err(s3_err(e, "GetObject current")),
        }
    }

    /// metadata に記録された全 release が参照する key (purge の対象)
    pub async fn recorded_release_keys(&self) -> Result<HashSet<String>> {
        let mut keys = HashSet::new();
        let prefix = format!("{}releases/", self.meta_prefix());
        let mut rel_keys: Vec<String> = self.list_prefix(&prefix).await?.into_keys().collect();
        rel_keys.sort();
        for key in rel_keys {
            let res = self
                .s3
                .get_object()
                .bucket(&self.bucket)
                .key(&key)
                .send()
                .await
                .map_err(|e| s3_err(e, &format!("GetObject {key}")))?;
            let data = res.body.collect().await?.into_bytes();
            let document = decode_release(&data)?;
            if !key.ends_with(&format!("/{}.json.gz", document.release)) {
                bail!(
                    "release 表 {key} の id ({}) が key と一致しません",
                    document.release
                );
            }
            for rel in document.entries.values() {
                if let Some(rel) = rel.as_str() {
                    keys.insert(format!("{}{}", self.key_prefix(), rel));
                }
            }
        }
        Ok(keys)
    }

    pub async fn purge(&self, dry_run: bool) -> Result<Vec<String>> {
        let existing = self.list_existing().await?;
        let mut keys: Vec<String> = self
            .recorded_release_keys()
            .await?
            .into_iter()
            .filter(|k| existing.contains_key(k))
            .collect();
        keys.sort();
        if dry_run || keys.is_empty() {
            return Ok(keys);
        }
        let mut failed: Vec<String> = Vec::new();
        for batch in keys.chunks(DELETE_BATCH) {
            let objects = batch
                .iter()
                .map(|k| ObjectIdentifier::builder().key(k).build())
                .collect::<std::result::Result<Vec<_>, _>>()?;
            let delete = Delete::builder()
                .set_objects(Some(objects))
                .quiet(true)
                .build()?;
            let res = self
                .s3
                .delete_objects()
                .bucket(&self.bucket)
                .delete(delete)
                .send()
                .await
                .map_err(|e| s3_err(e, "DeleteObjects"))?;
            // Quiet でも失敗した key は Errors に入る。HTTP 200 なので自分で見る
            failed.extend(describe_delete_errors(res.errors()));
        }
        if !failed.is_empty() {
            bail!(purge_failure_message(&failed));
        }
        Ok(keys)
    }
}

/// DeleteObjects の Errors を `key: Code Message` の形にする (Python 版と同文)
fn describe_delete_errors(errors: &[aws_sdk_s3::types::Error]) -> Vec<String> {
    errors
        .iter()
        .map(|err| {
            format!(
                "{}: {} {}",
                err.key().unwrap_or("?"),
                err.code().unwrap_or("?"),
                err.message().unwrap_or("")
            )
        })
        .collect()
}

/// DeleteObjects が一部の key を消せなかったときのエラー文 (Python 版と同文)
fn purge_failure_message(failed: &[String]) -> String {
    let lines: Vec<String> = failed.iter().map(|f| format!("  {f}")).collect();
    format!(
        "{} 件の削除に失敗しました (purge を再実行すると残りを対象にします):\n{}",
        failed.len(),
        lines.join("\n")
    )
}

fn describe(entry: &MapEntry) -> String {
    format!(
        "{} {} {}",
        &entry.sha256[..16],
        entry.source,
        if entry.hashed { "H" } else { "P" }
    )
}

fn check_rel_key_collisions(static_map: &StaticMap) -> Result<()> {
    let mut seen: HashMap<String, &str> = HashMap::new();
    for entry in static_map.sorted_entries() {
        let rel = entry.rel_key();
        if let Some(other) = seen.get(&rel) {
            bail!(
                "key {:?} が path {:?} と {:?} の両方から導かれます",
                rel,
                other,
                entry.path
            );
        }
        seen.insert(rel, &entry.path);
    }
    Ok(())
}

pub fn warn(message: &str) {
    eprintln!("{message}");
}

/// `pocket-rs static status` の表示用 (旧 Python 版の `status_report` と同じキー順)
pub async fn status_report(publisher: &StaticPublisher) -> Result<Value> {
    let mut report = Map::new();
    report.insert("route".into(), Value::String(publisher.route_label()));
    report.insert("bucket".into(), Value::String(publisher.bucket.clone()));
    report.insert(
        "key_prefix".into(),
        Value::String(publisher.key_prefix().to_string()),
    );
    report.insert(
        "map".into(),
        Value::String(publisher.map_file().display().to_string()),
    );
    let mut map_release: Option<String> = None;
    if publisher.map_file().is_file() {
        match publisher.read_map() {
            Ok(local) => {
                map_release = Some(local.release_id());
                report.insert("map_release".into(), Value::String(local.release_id()));
                report.insert("map_entries".into(), json!(local.entries.len()));
            }
            Err(e) => {
                report.insert("map_error".into(), Value::String(e.to_string()));
            }
        }
    }
    let current = publisher.current().await?;
    if let Some(current) = &current {
        report.insert(
            "current_release".into(),
            Value::String(current.release.clone()),
        );
        report.insert(
            "published_at".into(),
            current
                .published_at
                .clone()
                .map(Value::String)
                .unwrap_or(Value::Null),
        );
    }
    let in_sync = matches!((&current, &map_release), (Some(c), Some(m)) if &c.release == m);
    report.insert("in_sync".into(), Value::Bool(in_sync));
    Ok(Value::Object(report))
}

#[cfg(test)]
mod tests {
    use super::*;
    use aws_sdk_s3::error::{ConnectorError, ErrorMetadata};
    use aws_sdk_s3::operation::put_object::PutObjectError;
    use aws_sdk_s3::primitives::SdkBody;
    use std::cell::Cell;

    type PutErr = SdkError<PutObjectError, HttpResponse>;

    fn service_error(code: &str, status: u16) -> PutErr {
        let raw = HttpResponse::new(status.try_into().expect("status"), SdkBody::empty());
        SdkError::service_error(
            PutObjectError::generic(ErrorMetadata::builder().code(code).build()),
            raw,
        )
    }

    fn dispatch_failure() -> PutErr {
        SdkError::dispatch_failure(ConnectorError::io(
            std::io::Error::other("CONNECT tunnel failed").into(),
        ))
    }

    #[test]
    fn test_is_transient_classifies_sdk_errors() {
        assert!(is_transient(&dispatch_failure()));
        assert!(is_transient(
            &SdkError::<PutObjectError, HttpResponse>::timeout_error(std::io::Error::other(
                "timeout"
            ))
        ));
        assert!(is_transient(&service_error("SlowDown", 503)));
        assert!(is_transient(&service_error("InternalError", 500)));
        assert!(!is_transient(&service_error("PreconditionFailed", 412)));
        assert!(!is_transient(&service_error("AccessDenied", 403)));
        assert!(!is_transient(
            &SdkError::<PutObjectError, HttpResponse>::construction_failure(std::io::Error::other(
                "no such file"
            ))
        ));
    }

    #[test]
    fn test_retry_transient_retries_only_transient_errors() {
        let rt = tokio::runtime::Runtime::new().expect("runtime");
        // 2 回瞬断して 3 回目で成功
        let calls = Cell::new(0);
        let result: std::result::Result<&str, PutErr> =
            rt.block_on(retry_transient("PutObject k", 3, Duration::ZERO, || {
                calls.set(calls.get() + 1);
                let n = calls.get();
                async move {
                    if n <= 2 {
                        Err(dispatch_failure())
                    } else {
                        Ok("ok")
                    }
                }
            }));
        assert_eq!(result.expect("recovered"), "ok");
        assert_eq!(calls.get(), 3);
        // 試行回数を使い切ったら最後のエラーを返す
        let calls = Cell::new(0);
        let result: std::result::Result<&str, PutErr> =
            rt.block_on(retry_transient("PutObject k", 3, Duration::ZERO, || {
                calls.set(calls.get() + 1);
                async { Err(dispatch_failure()) }
            }));
        assert!(matches!(result, Err(SdkError::DispatchFailure(_))));
        assert_eq!(calls.get(), 3);
        // 412 は再試行しない
        let calls = Cell::new(0);
        let result: std::result::Result<&str, PutErr> =
            rt.block_on(retry_transient("PutObject k", 3, Duration::ZERO, || {
                calls.set(calls.get() + 1);
                async { Err(service_error("PreconditionFailed", 412)) }
            }));
        assert!(is_precondition_failed(&result.unwrap_err()));
        assert_eq!(calls.get(), 1);
    }

    #[test]
    fn test_purge_failure_message_lists_failed_keys() {
        let errors = vec![
            aws_sdk_s3::types::Error::builder()
                .key("static/img/a.0123456789abcdef.jpg")
                .code("AccessDenied")
                .message("Access Denied")
                .build(),
            aws_sdk_s3::types::Error::builder()
                .key("static/b.txt")
                .build(),
        ];
        let failed = describe_delete_errors(&errors);
        assert_eq!(
            failed,
            vec![
                "static/img/a.0123456789abcdef.jpg: AccessDenied Access Denied",
                "static/b.txt: ? ",
            ]
        );
        assert_eq!(
            purge_failure_message(&failed),
            "2 件の削除に失敗しました (purge を再実行すると残りを対象にします):\n  \
             static/img/a.0123456789abcdef.jpg: AccessDenied Access Denied\n  static/b.txt: ? "
        );
    }
}
