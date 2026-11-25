//! CloudFront の bucket への upload (Python 版 `resources/cloudfront.py` の `upload` /
//! `upload_managed_assets`)。
//!
//! route の `upload_dir` を S3 の prefix に同期する。内容が同じ object は上げ直さない
//! (S3 の ETag とローカルの MD5 を比べる)。変更があった route だけを invalidation する。
//!
//! Python 版との違い:
//! - upload は大きさによらず単一の PUT (Python 版は 128MB 超を multipart にし、その
//!   object は毎回上げ直していた)。5GB を超えるファイルはエラー
//! - Content-Type は `mime_guess` で決める (Python 版は `mimetypes`)
//! - `upload_dir` が無ければエラー (Python 版は空と見なし、`delete_stale` なら prefix の
//!   object を全部消した)
//! - invalidation は SDK を使わず、署名した HTTP で呼ぶ (CloudFront の SDK は rustc が
//!   2.4GB を使い、使う API はこれ 1 つ)

use std::collections::{BTreeMap, BTreeSet};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

use anyhow::{anyhow, bail, Context as _, Result};
use aws_config::SdkConfig;
use aws_sdk_s3::config::ProvideCredentials;
use aws_sdk_s3::primitives::ByteStream;
use aws_sdk_s3::Client;
use aws_sigv4::http_request::{sign, SignableBody, SignableRequest, SigningSettings};
use aws_sigv4::sign::v4;
use magic_pocket_settings::context::{CloudFrontContext, RouteContext};
use md5::{Digest, Md5};

use crate::aws_util::sdk_error;
use crate::publisher::content_type_of;
use crate::status::sink::Sink;

/// 単一の PUT で上げられる大きさの上限
const MAX_PUT_BYTES: u64 = 5 * 1024 * 1024 * 1024;
const MANAGED_PREFIX: &str = "pocket_managed/";

/// route の object を置く S3 の prefix (前後に `/` は付かない)
pub fn s3_prefix(route: &RouteContext) -> String {
    let pattern = route.path_pattern.trim_end_matches(['/', '*']);
    format!("{}{pattern}", route.origin_path)
        .trim_start_matches('/')
        .to_string()
}

/// prefix と相対 path をつないだ key (prefix が空なら bucket の直下)
fn join_key(prefix: &str, relative: &str) -> String {
    if prefix.is_empty() {
        relative.to_string()
    } else {
        format!("{prefix}/{relative}")
    }
}

/// prefix 配下を list するときの prefix (空なら bucket 全体)
fn list_prefix(prefix: &str) -> String {
    join_key(prefix, "")
}

/// `dir` 配下のファイルを {S3 の key: ローカルの path} で返す。
///
/// ディレクトリへの symlink はたどらない (Python の `rglob` と同じ)
pub fn local_files(dir: &Path, prefix: &str) -> Result<BTreeMap<String, PathBuf>> {
    fn walk(
        root: &Path,
        dir: &Path,
        prefix: &str,
        files: &mut BTreeMap<String, PathBuf>,
    ) -> Result<()> {
        let entries =
            std::fs::read_dir(dir).with_context(|| format!("{} を読めません", dir.display()))?;
        for entry in entries {
            let entry = entry?;
            let path = entry.path();
            if path.is_dir() {
                if !entry.file_type()?.is_symlink() {
                    walk(root, &path, prefix, files)?;
                }
                continue;
            }
            let relative = path.strip_prefix(root)?;
            let parts: Vec<String> = relative
                .components()
                .map(|part| part.as_os_str().to_string_lossy().into_owned())
                .collect();
            files.insert(join_key(prefix, &parts.join("/")), path);
        }
        Ok(())
    }
    if !dir.is_dir() {
        bail!(
            "upload_dir '{}' がありません。build の出力先を確認してください。",
            dir.display()
        );
    }
    let mut files = BTreeMap::new();
    walk(dir, dir, prefix, &mut files)?;
    Ok(files)
}

/// object に付ける Cache-Control。付けないなら `None`
pub fn cache_control(route: &RouteContext, file: &Path) -> Option<String> {
    if route.is_content_hash {
        // viewer-response Function が viewer に付ける値と同じ。object にも付けないと
        // edge のキャッシュが cache policy の既定 (1 日) で切れる
        return Some(format!(
            "public, max-age={}, immutable",
            route.versioned_max_age
        ));
    }
    if route.is_spa {
        let html = matches!(
            file.extension().and_then(|ext| ext.to_str()),
            Some("html" | "htm")
        );
        return Some(
            if html {
                "no-cache, no-store"
            } else {
                "max-age=31536000"
            }
            .to_string(),
        );
    }
    None
}

/// S3 の object がローカルのファイルと同じ内容と言い切れるか。
///
/// 言い切れるのは ETag が素の MD5 (単一の PUT で上げた object) で、ローカルの MD5 と
/// 一致するときだけ。multipart や SSE-KMS の ETag は MD5 ではないので、変更ありとして
/// 上げ直す。大きさだけの比較はしない (同じ大きさで内容が違うファイルを見逃す)
pub fn is_unchanged(local_md5: &str, etag: Option<&str>) -> bool {
    matches!(etag, Some(etag) if !etag.contains('-') && etag == local_md5)
}

fn md5_hex(file: &Path) -> Result<String> {
    let mut reader =
        std::fs::File::open(file).with_context(|| format!("{} を開けません", file.display()))?;
    let mut md5 = Md5::new();
    let mut buffer = vec![0u8; 1024 * 1024];
    loop {
        let read = reader.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        md5.update(&buffer[..read]);
    }
    Ok(hex::encode(md5.finalize()))
}

/// invalidation する path (変更があった route の path_pattern。重複を除いた昇順)
pub fn invalidation_paths(routes: &[&RouteContext]) -> Vec<String> {
    let paths: BTreeSet<String> = routes
        .iter()
        .map(|route| {
            if route.path_pattern.is_empty() {
                "/*".to_string()
            } else {
                route.path_pattern.clone()
            }
        })
        .collect();
    paths.into_iter().collect()
}

/// managed_assets の置き場 (`<base>/<stage>/` があればそれ、無ければ `<base>/default/`)
pub fn managed_assets_dir(base: &Path, stage: &str) -> PathBuf {
    let stage_dir = base.join(stage);
    if stage_dir.is_dir() {
        stage_dir
    } else {
        base.join("default")
    }
}

/// prefix 配下の {key: ETag} (ETag の前後の `"` は除く)
async fn list_objects(
    client: &Client,
    bucket: &str,
    prefix: &str,
) -> Result<BTreeMap<String, String>> {
    let mut pages = client
        .list_objects_v2()
        .bucket(bucket)
        .prefix(prefix)
        .into_paginator()
        .send();
    let mut objects = BTreeMap::new();
    while let Some(page) = pages.next().await {
        let page = page.map_err(|e| sdk_error("s3 list_objects_v2", &e))?;
        for object in page.contents() {
            if let Some(key) = object.key() {
                let etag = object.e_tag().unwrap_or_default().trim_matches('"');
                objects.insert(key.to_string(), etag.to_string());
            }
        }
    }
    Ok(objects)
}

async fn put_object(
    client: &Client,
    bucket: &str,
    key: &str,
    file: &Path,
    cache_control: Option<String>,
) -> Result<()> {
    let size = std::fs::metadata(file)?.len();
    if size > MAX_PUT_BYTES {
        bail!(
            "{} は 5GB を超えているため upload できません ({size} bytes)。",
            file.display()
        );
    }
    let body = ByteStream::from_path(file)
        .await
        .with_context(|| format!("{} を読めません", file.display()))?;
    client
        .put_object()
        .bucket(bucket)
        .key(key)
        .body(body)
        .content_type(content_type_of(&file.to_string_lossy()))
        .set_cache_control(cache_control)
        .send()
        .await
        .map_err(|e| sdk_error("s3 put_object", &e))?;
    Ok(())
}

async fn delete_object(client: &Client, bucket: &str, key: &str) -> Result<()> {
    client
        .delete_object()
        .bucket(bucket)
        .key(key)
        .send()
        .await
        .map_err(|e| sdk_error("s3 delete_object", &e))?;
    Ok(())
}

/// [`sync_dir`] の指定
#[derive(Debug, Clone, Copy, Default)]
pub struct SyncOptions {
    /// prefix 配下でローカルに無い key を消す
    pub delete: bool,
    /// `delete` のとき、ローカルのファイルが 1 つも無ければ消さずにエラーにする
    /// (build の失敗や path の誤りで prefix を丸ごと消さないため)
    pub refuse_empty_delete: bool,
    /// `.` で始まる要素を path に含むファイルを除く (`aws s3 sync --exclude ".*"
    /// --exclude "*/.*"` と同じ。S3 側の該当 key も消さない)
    pub exclude_hidden: bool,
    /// 上げる・消す key を表示するだけで、S3 には書かない
    pub dryrun: bool,
}

/// [`sync_dir`] の結果
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SyncResult {
    pub uploaded: usize,
    pub skipped: usize,
    /// prefix 配下でローカルに無い key (`delete` なら消した key)
    pub stale: Vec<String>,
}

/// key の prefix より後ろに `.` で始まる要素があるか
fn is_hidden(key: &str, prefix: &str) -> bool {
    let relative = key.strip_prefix(&list_prefix(prefix)).unwrap_or(key);
    relative.split('/').any(|part| part.starts_with('.'))
}

/// ローカルの `dir` を `s3://<bucket>/<prefix>/` に同期する。内容 (ETag とローカルの MD5)
/// が同じ object は上げ直さない。
///
/// route の upload (`Uploader`) と Django の deploystatic / storage upload が使う
pub async fn sync_dir(
    client: &Client,
    bucket: &str,
    dir: &Path,
    prefix: &str,
    cache_control: &dyn Fn(&Path) -> Option<String>,
    options: SyncOptions,
    sink: &mut dyn Sink,
) -> Result<SyncResult> {
    let mut files = local_files(dir, prefix)?;
    let mut existing = list_objects(client, bucket, &list_prefix(prefix)).await?;
    if options.exclude_hidden {
        files.retain(|key, _| !is_hidden(key, prefix));
        existing.retain(|key, _| !is_hidden(key, prefix));
    }
    if options.delete && options.refuse_empty_delete && files.is_empty() {
        bail!(
            "{} にファイルがありません。S3 の object を消さずに止めます              (collectstatic の出力先を確認してください)。",
            dir.display()
        );
    }
    let mut result = SyncResult::default();
    for (key, file) in &files {
        if is_unchanged(&md5_hex(file)?, existing.get(key).map(String::as_str)) {
            result.skipped += 1;
            continue;
        }
        if options.dryrun {
            sink.log(&format!("(dryrun) アップロード: s3://{bucket}/{key}"));
        } else {
            put_object(client, bucket, key, file, cache_control(file)).await?;
            sink.log(&format!("アップロード: s3://{bucket}/{key}"));
        }
        result.uploaded += 1;
    }
    result.stale = existing
        .keys()
        .filter(|key| !files.contains_key(*key))
        .cloned()
        .collect();
    if options.delete {
        for key in &result.stale {
            if options.dryrun {
                sink.log(&format!("(dryrun) 削除: s3://{bucket}/{key}"));
            } else {
                delete_object(client, bucket, key).await?;
                sink.log(&format!("削除: s3://{bucket}/{key}"));
            }
        }
    }
    Ok(result)
}

/// cloudfront 1 つの bucket への upload
pub struct Uploader<'a> {
    pub sdk: &'a SdkConfig,
    pub client: &'a Client,
    pub cf: &'a CloudFrontContext,
    /// pocket.toml のあるディレクトリ (相対 path と build コマンドの起点)
    pub project_dir: &'a Path,
}

impl Uploader<'_> {
    fn bucket(&self) -> &str {
        &self.cf.bucket_name
    }

    async fn list_objects(&self, prefix: &str) -> Result<BTreeMap<String, String>> {
        list_objects(self.client, self.bucket(), prefix).await
    }

    async fn put(&self, key: &str, file: &Path, cache_control: Option<String>) -> Result<()> {
        put_object(self.client, self.bucket(), key, file, cache_control).await
    }

    async fn delete(&self, key: &str) -> Result<()> {
        delete_object(self.client, self.bucket(), key).await
    }

    /// managed_assets のファイルを `pocket_managed/` に同期する (全件 upload + 不要な
    /// object の削除)
    pub async fn upload_managed_assets(&self, sink: &mut dyn Sink) -> Result<()> {
        let Some(base) = self.cf.managed_assets.as_deref().filter(|m| !m.is_empty()) else {
            return Ok(());
        };
        let dir = managed_assets_dir(&self.project_dir.join(base), &self.cf.stage);
        if !dir.is_dir() {
            sink.warning(&format!(
                "managed_assets ディレクトリが見つかりません: {}",
                dir.display()
            ));
            return Ok(());
        }
        let bucket = self.bucket();
        let mut uploaded = BTreeSet::new();
        let entries =
            std::fs::read_dir(&dir).with_context(|| format!("{} を読めません", dir.display()))?;
        for entry in entries {
            let path = entry?.path();
            if !path.is_file() {
                continue;
            }
            let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
                continue;
            };
            let key = format!("{MANAGED_PREFIX}{name}");
            self.put(&key, &path, None).await?;
            sink.log(&format!("managed_assets: s3://{bucket}/{key}"));
            uploaded.insert(key);
        }
        for key in self.list_objects(MANAGED_PREFIX).await?.keys() {
            if !uploaded.contains(key) {
                self.delete(key).await?;
                sink.log(&format!("managed_assets 削除: s3://{bucket}/{key}"));
            }
        }
        sink.info(&format!(
            "managed_assets: {} ファイルをアップロードしました",
            uploaded.len()
        ));
        Ok(())
    }

    /// route を build して S3 に同期し、変更があった route を invalidation する
    pub async fn upload(
        &self,
        routes: &[&RouteContext],
        skip_build: bool,
        distribution_id: &str,
        sink: &mut dyn Sink,
    ) -> Result<()> {
        let mut changed = Vec::new();
        for route in routes {
            if let Some(command) = route.build_cmd.as_deref().filter(|_| !skip_build) {
                self.build(command, sink)?;
            }
            if self.upload_route(route, sink).await? {
                changed.push(*route);
            }
        }
        if changed.is_empty() {
            return Ok(());
        }
        let paths = invalidation_paths(&changed);
        create_invalidation(self.sdk, distribution_id, &paths).await?;
        sink.info(&format!(
            "CloudFront キャッシュ無効化をリクエストしました: {}",
            paths.join(", ")
        ));
        Ok(())
    }

    /// pocket.toml の build.cmd を shell で実行する (相対 path は pocket.toml の
    /// ディレクトリが起点)
    fn build(&self, command: &str, sink: &mut dyn Sink) -> Result<()> {
        sink.info(&format!("ビルド実行: {command}"));
        let status = std::process::Command::new("sh")
            .arg("-c")
            .arg(command)
            .current_dir(self.project_dir)
            .status()
            .with_context(|| format!("build コマンドを起動できません: {command}"))?;
        if status.success() {
            return Ok(());
        }
        let code = status.code().unwrap_or(-1);
        sink.danger(&format!(
            "build コマンドが失敗しました (exit {code}): {command}"
        ));
        sink.warning(
            "deploy ホスト側で依存を入れ直してから再実行してください。 \
             例えば npm/bun の optional dependency (rolldown 等) \
             が materialize されていないと、ロックファイルにあっても \
             import 時に Cannot find module で失敗します \
             (`rm -rf node_modules && npm ci` 等で復旧)。",
        );
        bail!("build コマンドが失敗しました (exit {code}): {command}")
    }

    /// route の upload_dir を S3 に同期する。upload か削除があれば `true`。
    ///
    /// 今回の成果物に無い key は `delete_stale` のときだけ消す
    async fn upload_route(&self, route: &RouteContext, sink: &mut dyn Sink) -> Result<bool> {
        let upload_dir = route
            .upload_dir
            .as_deref()
            .filter(|dir| !dir.is_empty())
            .context("route.upload_dir is not set")?;
        let prefix = s3_prefix(route);
        let delete = route.delete_stale == Some(true);
        let result = sync_dir(
            self.client,
            self.bucket(),
            &self.project_dir.join(upload_dir),
            &prefix,
            &|file| cache_control(route, file),
            SyncOptions {
                delete,
                ..SyncOptions::default()
            },
            sink,
        )
        .await?;
        let (uploaded, skipped, stale) = (result.uploaded, result.skipped, result.stale);
        if delete {
            sink.info(&format!(
                "{uploaded} ファイルをアップロードしました (skip {skipped} / 削除 {}、prefix: {prefix})",
                stale.len()
            ));
            return Ok(uploaded > 0 || !stale.is_empty());
        }
        sink.info(&format!(
            "{uploaded} ファイルをアップロードしました (skip {skipped} / 旧版を保持 {}、prefix: {prefix})",
            stale.len()
        ));
        Ok(uploaded > 0)
    }
}

fn xml_escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

/// CreateInvalidation の body
fn invalidation_body(paths: &[String], caller_reference: &str) -> String {
    let items: String = paths
        .iter()
        .map(|path| format!("<Path>{}</Path>", xml_escape(path)))
        .collect();
    format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\
         <InvalidationBatch xmlns=\"http://cloudfront.amazonaws.com/doc/2020-05-31/\">\
         <Paths><Quantity>{}</Quantity><Items>{items}</Items></Paths>\
         <CallerReference>{}</CallerReference>\
         </InvalidationBatch>",
        paths.len(),
        xml_escape(caller_reference)
    )
}

/// CloudFront の CreateInvalidation を呼ぶ (署名した HTTP)。
///
/// CloudFront は global service で、署名の region は us-east-1 固定。endpoint は
/// `AWS_ENDPOINT_URL` (SDK の設定) があればそれを使う
pub async fn create_invalidation(
    sdk: &SdkConfig,
    distribution_id: &str,
    paths: &[String],
) -> Result<()> {
    let endpoint = sdk
        .endpoint_url()
        .unwrap_or("https://cloudfront.amazonaws.com")
        .trim_end_matches('/');
    let url = format!("{endpoint}/2020-05-31/distribution/{distribution_id}/invalidation");
    let now = SystemTime::now();
    let seconds = now.duration_since(SystemTime::UNIX_EPOCH)?.as_secs();
    let body = invalidation_body(paths, &seconds.to_string());
    let credentials = sdk
        .credentials_provider()
        .context("AWS の認証情報がありません")?
        .provide_credentials()
        .await
        .map_err(|e| anyhow!("AWS の認証情報を取得できません: {e}"))?;
    let identity = credentials.into();
    let params = v4::SigningParams::builder()
        .identity(&identity)
        .region("us-east-1")
        .name("cloudfront")
        .time(now)
        .settings(SigningSettings::default())
        .build()
        .map_err(|e| anyhow!("署名の設定を組めません: {e}"))?
        .into();
    let headers = [("content-type", "text/xml")];
    let signable = SignableRequest::new(
        "POST",
        url.as_str(),
        headers.iter().copied(),
        SignableBody::Bytes(body.as_bytes()),
    )
    .map_err(|e| anyhow!("署名する request を組めません: {e}"))?;
    let (instructions, _signature) = sign(signable, &params)
        .map_err(|e| anyhow!("request に署名できません: {e}"))?
        .into_parts();
    let mut request = reqwest::Client::new().post(&url);
    for (name, value) in headers {
        request = request.header(name, value);
    }
    for (name, value) in instructions.headers() {
        request = request.header(name, value);
    }
    let response = request
        .body(body)
        .send()
        .await
        .map_err(|e| anyhow!("cloudfront create_invalidation: {e}"))?;
    let status = response.status();
    if !status.is_success() {
        let text = response.text().await.unwrap_or_default();
        bail!("cloudfront create_invalidation: {status}: {text}");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stack::testing::vector_context;

    fn route<'c>(cf: &'c CloudFrontContext, pattern: &str) -> &'c RouteContext {
        cf.routes
            .iter()
            .find(|route| route.path_pattern == pattern)
            .expect("route")
    }

    fn web() -> CloudFrontContext {
        let ctx = vector_context("toml_vectors/cloudfront_full.dev");
        ctx.cloudfront.get("web").expect("web").clone()
    }

    #[test]
    fn test_s3_prefix() {
        let cf = web();
        // origin_path + path_pattern (末尾の /* を除く)。先頭の / は付けない
        assert_eq!(s3_prefix(route(&cf, "")), "spa");
        assert_eq!(s3_prefix(route(&cf, "/assets/*")), "assets");
        assert_eq!(s3_prefix(route(&cf, "/media/*")), "uploads/media");
    }

    #[test]
    fn test_cache_control() {
        let cf = web();
        let spa = route(&cf, "");
        assert_eq!(
            cache_control(spa, Path::new("a/index.html")).as_deref(),
            Some("no-cache, no-store")
        );
        assert_eq!(
            cache_control(spa, Path::new("x.htm")).as_deref(),
            Some("no-cache, no-store")
        );
        assert_eq!(
            cache_control(spa, Path::new("app.js")).as_deref(),
            Some("max-age=31536000")
        );
        assert_eq!(
            cache_control(route(&cf, "/assets/*"), Path::new("a.html")).as_deref(),
            Some("public, max-age=3600, immutable")
        );
        assert_eq!(
            cache_control(route(&cf, "/deploy/*"), Path::new("a.js")),
            None
        );
    }

    #[test]
    fn test_is_unchanged_requires_plain_md5_etag() {
        assert!(is_unchanged("abc", Some("abc")));
        assert!(!is_unchanged("abc", Some("abd")));
        assert!(!is_unchanged("abc", None));
        // multipart の ETag は内容が同じでも変更ありとする
        assert!(!is_unchanged("abc-2", Some("abc-2")));
    }

    #[test]
    fn test_local_files_walks_recursively_and_requires_directory() {
        let dir = tempfile::tempdir().expect("tempdir");
        let root = dir.path().join("dist");
        std::fs::create_dir_all(root.join("js/chunks")).expect("mkdir");
        std::fs::create_dir_all(root.join("empty")).expect("mkdir");
        std::fs::write(root.join("index.html"), "x").expect("write");
        std::fs::write(root.join("js/chunks/a.js"), "y").expect("write");
        let files = local_files(&root, "spa").expect("files");
        let keys: Vec<&str> = files.keys().map(String::as_str).collect();
        assert_eq!(keys, ["spa/index.html", "spa/js/chunks/a.js"]);
        assert_eq!(files["spa/js/chunks/a.js"], root.join("js/chunks/a.js"));
        // 空のディレクトリは「ファイル無し」、存在しないディレクトリはエラー
        assert!(local_files(&root.join("empty"), "spa")
            .expect("空")
            .is_empty());
        let error = local_files(&dir.path().join("missing"), "spa").expect_err("無い");
        assert!(error.to_string().contains("がありません"), "{error}");
    }

    #[test]
    fn test_md5_hex() {
        let dir = tempfile::tempdir().expect("tempdir");
        let file = dir.path().join("a.txt");
        std::fs::write(&file, "hello").expect("write");
        assert_eq!(
            md5_hex(&file).expect("md5"),
            "5d41402abc4b2a76b9719d911017c592"
        );
    }

    #[test]
    fn test_invalidation_paths_are_sorted_and_unique() {
        let cf = web();
        let routes = [
            route(&cf, "/deploy/*"),
            route(&cf, ""),
            route(&cf, "/deploy/*"),
        ];
        assert_eq!(invalidation_paths(&routes), ["/*", "/deploy/*"]);
    }

    #[test]
    fn test_managed_assets_dir_prefers_stage() {
        let dir = tempfile::tempdir().expect("tempdir");
        let base = dir.path().join("fonts");
        std::fs::create_dir_all(base.join("default")).expect("mkdir");
        assert_eq!(managed_assets_dir(&base, "dev"), base.join("default"));
        std::fs::create_dir_all(base.join("dev")).expect("mkdir");
        assert_eq!(managed_assets_dir(&base, "dev"), base.join("dev"));
    }

    #[test]
    fn test_invalidation_body() {
        let body = invalidation_body(&["/*".to_string(), "/a&b/*".to_string()], "1700000000");
        assert_eq!(
            body,
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\
             <InvalidationBatch xmlns=\"http://cloudfront.amazonaws.com/doc/2020-05-31/\">\
             <Paths><Quantity>2</Quantity><Items><Path>/*</Path><Path>/a&amp;b/*</Path></Items></Paths>\
             <CallerReference>1700000000</CallerReference>\
             </InvalidationBatch>"
        );
    }
}
