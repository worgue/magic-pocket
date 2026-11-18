//! S3 bucket の作成・設定・削除 (Python 版 `pocket_cli/resources/s3.py` と
//! `resources/aws/s3_utils.py`)。
//!
//! 設定 (public access block / CORS / versioning / lifecycle) は宣言的に合わせる:
//! 望む値と今の値が違えば書き、望む値が無く今の値があれば消し、同じなら API を呼ばない。
//! 望む値は status と同じ関数 (`status/s3.rs`) で組む。

use std::time::Duration;

use anyhow::{anyhow, bail, Context as _, Result};
use aws_sdk_s3::types::{
    BucketLifecycleConfiguration, BucketLocationConstraint, BucketVersioningStatus,
    CorsConfiguration, CorsRule, CreateBucketConfiguration, Delete, ExpirationStatus,
    LifecycleExpiration, LifecycleRule, LifecycleRuleFilter, NoncurrentVersionExpiration,
    ObjectIdentifier, PublicAccessBlockConfiguration, VersioningConfiguration,
};
use aws_sdk_s3::Client;
use magic_pocket_settings::context::{CloudFrontContext, S3Context};
use magic_pocket_settings::types::OrderedMap;
use serde_json::{json, Value as Json};

use crate::aws_util::{has_code, sdk_error};
use crate::status::s3 as s3_status;
use crate::status::sink::Sink;

/// 宣言的な設定 1 つに対して行う操作
#[derive(Debug, Clone, PartialEq)]
pub enum Change {
    Keep,
    Put(Json),
    Delete,
}

/// 望む値と今の値から操作を決める (Python の `_ensure_cors` / `_ensure_lifecycle`)
pub fn declared_change(desired: Option<Json>, current: Option<&Json>) -> Change {
    match desired {
        desired if desired.as_ref() == current => Change::Keep,
        None => Change::Delete,
        Some(desired) => Change::Put(desired),
    }
}

/// versioning に書く状態。変えないなら `None`。
///
/// 未設定と Suspended はどちらも「有効でない」として同じに扱う (一度 Enabled にした
/// bucket は Suspended にしか戻せない)
pub fn versioning_change(want: bool, current: Option<&str>) -> Option<&'static str> {
    match (want, current == Some("Enabled")) {
        (true, false) => Some("Enabled"),
        (false, true) => Some("Suspended"),
        _ => None,
    }
}

/// 望む public access block (Python の `public_access_block_should_be`)
pub fn public_access_block_should_be() -> Json {
    json!({
        "BlockPublicAcls": true,
        "IgnorePublicAcls": true,
        "BlockPublicPolicy": true,
        "RestrictPublicBuckets": true,
    })
}

/// bucket が在るか。404 以外の応答は `Err` (他の account の bucket など)
pub async fn bucket_exists(client: &Client, bucket: &str) -> Result<bool> {
    match client.head_bucket().bucket(bucket).send().await {
        Ok(_) => Ok(true),
        Err(e) if e.raw_response().is_some_and(|r| r.status().as_u16() == 404) => Ok(false),
        Err(e) => Err(sdk_error("s3 head_bucket", &e)),
    }
}

/// 作成の再試行 (削除直後の同名 bucket は、削除の伝播が終わるまで `OperationAborted` で
/// 拒否される。AWS の文書では最大 1 時間程度)
#[derive(Debug, Clone, Copy)]
pub struct CreateRetry {
    pub interval: Duration,
    pub attempts: u64,
}

impl Default for CreateRetry {
    fn default() -> Self {
        Self {
            interval: Duration::from_secs(30),
            attempts: 120,
        }
    }
}

impl CreateRetry {
    fn minutes(&self, attempts: u64) -> u64 {
        self.interval.as_secs() * attempts / 60
    }
}

/// 1 回の作成の失敗
pub enum CreateError {
    /// 削除の伝播待ち (再試行する)
    Aborted,
    Other(anyhow::Error),
}

/// `OperationAborted` の間だけ再試行する。他の失敗はすぐ返す
pub async fn create_with_retry<F, Fut>(
    bucket: &str,
    retry: CreateRetry,
    sink: &mut dyn Sink,
    mut create_once: F,
) -> Result<()>
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = Result<(), CreateError>>,
{
    let mut failed = 0;
    loop {
        match create_once().await {
            Ok(()) => return Ok(()),
            Err(CreateError::Other(error)) => return Err(error),
            Err(CreateError::Aborted) => {}
        }
        failed += 1;
        if failed > retry.attempts {
            bail!(
                "bucket '{bucket}' の作成が OperationAborted のまま {} 分以内に成功\
                 しませんでした。削除直後の同名 bucket は削除の伝播 (最大 1 時間\
                 程度) が終わるまで再作成できません。時間を置いて再実行して\
                 ください。",
                retry.minutes(retry.attempts)
            );
        }
        sink.warning(&format!(
            "bucket '{bucket}' の作成が OperationAborted で拒否されました (削除直後の\
             同名 bucket は削除の伝播が終わるまで再作成できません)。{} 秒後に\
             再試行します (最長あと {} 分待ちます)...",
            retry.interval.as_secs(),
            retry.minutes(retry.attempts + 1 - failed)
        ));
        tokio::time::sleep(retry.interval).await;
    }
}

async fn create_bucket_once(
    client: &Client,
    bucket: &str,
    region: &str,
) -> Result<(), CreateError> {
    let mut request = client.create_bucket().bucket(bucket);
    if region != "us-east-1" {
        request = request.create_bucket_configuration(
            CreateBucketConfiguration::builder()
                .location_constraint(BucketLocationConstraint::from(region))
                .build(),
        );
    }
    match request.send().await {
        Ok(_) => Ok(()),
        Err(e) if has_code(&e, &["OperationAborted"]) => Err(CreateError::Aborted),
        Err(e) => Err(CreateError::Other(sdk_error("s3 create_bucket", &e))),
    }
}

/// bucket 1 つの書き込み
pub struct BucketWriter<'a> {
    pub client: &'a Client,
    pub s3: &'a S3Context,
    pub cloudfront: &'a OrderedMap<CloudFrontContext>,
    pub retry: CreateRetry,
}

impl BucketWriter<'_> {
    fn bucket(&self) -> &str {
        &self.s3.bucket_name
    }

    /// bucket が在るか。404 以外の応答は、他の account が同じ名前を使っている見込み
    pub async fn exists(&self) -> Result<bool> {
        bucket_exists(self.client, self.bucket())
            .await
            .context("Bucket might be already used by other account. Try another bucket_prefix.")
    }

    pub async fn create(&self, sink: &mut dyn Sink) -> Result<()> {
        let bucket = self.bucket();
        create_with_retry(bucket, self.retry, sink, || {
            create_bucket_once(self.client, bucket, &self.s3.region)
        })
        .await?;
        self.update(sink).await
    }

    /// 4 つの設定を望む値に合わせる
    pub async fn update(&self, sink: &mut dyn Sink) -> Result<()> {
        self.ensure_public_access_block(sink).await?;
        self.ensure_cors(sink).await?;
        self.ensure_versioning(sink).await?;
        self.ensure_lifecycle(sink).await
    }

    async fn ensure_public_access_block(&self, sink: &mut dyn Sink) -> Result<()> {
        let bucket = self.bucket();
        let current = match self
            .client
            .get_public_access_block()
            .bucket(bucket)
            .send()
            .await
        {
            Ok(response) => response.public_access_block_configuration.map(|b| {
                json!({
                    "BlockPublicAcls": b.block_public_acls,
                    "IgnorePublicAcls": b.ignore_public_acls,
                    "BlockPublicPolicy": b.block_public_policy,
                    "RestrictPublicBuckets": b.restrict_public_buckets,
                })
            }),
            Err(e) if has_code(&e, &["NoSuchPublicAccessBlockConfiguration"]) => None,
            Err(e) => return Err(sdk_error("s3 get_public_access_block", &e)),
        };
        let desired = public_access_block_should_be();
        if current.as_ref() == Some(&desired) {
            sink.info("Public access block is already configured properly.");
            return Ok(());
        }
        sink.info("Update public access block configuration");
        sink.info(&format!(
            "Current configuration: {}",
            current.unwrap_or(Json::Null)
        ));
        self.client
            .put_public_access_block()
            .bucket(bucket)
            .public_access_block_configuration(
                PublicAccessBlockConfiguration::builder()
                    .block_public_acls(true)
                    .ignore_public_acls(true)
                    .block_public_policy(true)
                    .restrict_public_buckets(true)
                    .build(),
            )
            .send()
            .await
            .map_err(|e| sdk_error("s3 put_public_access_block", &e))?;
        sink.info(&format!("Updated to: {desired}"));
        Ok(())
    }

    async fn ensure_cors(&self, sink: &mut dyn Sink) -> Result<()> {
        let bucket = self.bucket();
        let desired = s3_status::desired_cors(self.s3, self.cloudfront, sink);
        let current = match self.client.get_bucket_cors().bucket(bucket).send().await {
            Ok(response) => Some(s3_status::cors_rules_json(response.cors_rules())),
            Err(e) if has_code(&e, &["NoSuchCORSConfiguration"]) => None,
            Err(e) => return Err(sdk_error("s3 get_bucket_cors", &e)),
        };
        match declared_change(desired, current.as_ref()) {
            Change::Keep => {}
            Change::Delete => {
                self.client
                    .delete_bucket_cors()
                    .bucket(bucket)
                    .send()
                    .await
                    .map_err(|e| sdk_error("s3 delete_bucket_cors", &e))?;
                sink.info("CORS 設定を削除しました");
            }
            Change::Put(rules) => {
                self.client
                    .put_bucket_cors()
                    .bucket(bucket)
                    .cors_configuration(cors_configuration(&rules)?)
                    .send()
                    .await
                    .map_err(|e| sdk_error("s3 put_bucket_cors", &e))?;
                sink.info(&format!(
                    "CORS 設定を適用しました: {}",
                    py_str_list(&strings(&rules[0]["AllowedOrigins"]))
                ));
            }
        }
        Ok(())
    }

    async fn ensure_versioning(&self, sink: &mut dyn Sink) -> Result<()> {
        let bucket = self.bucket();
        let response = self
            .client
            .get_bucket_versioning()
            .bucket(bucket)
            .send()
            .await
            .map_err(|e| sdk_error("s3 get_bucket_versioning", &e))?;
        let current = response.status().map(|status| status.as_str());
        let Some(status) = versioning_change(self.s3.versioning, current) else {
            return Ok(());
        };
        self.client
            .put_bucket_versioning()
            .bucket(bucket)
            .versioning_configuration(
                VersioningConfiguration::builder()
                    .status(BucketVersioningStatus::from(status))
                    .build(),
            )
            .send()
            .await
            .map_err(|e| sdk_error("s3 put_bucket_versioning", &e))?;
        sink.info(if status == "Enabled" {
            "Versioning を有効化しました"
        } else {
            "Versioning を Suspended にしました"
        });
        Ok(())
    }

    async fn ensure_lifecycle(&self, sink: &mut dyn Sink) -> Result<()> {
        let bucket = self.bucket();
        let desired = s3_status::desired_lifecycle(self.s3);
        let current = match self
            .client
            .get_bucket_lifecycle_configuration()
            .bucket(bucket)
            .send()
            .await
        {
            Ok(response) => Some(s3_status::lifecycle_rules_json(response.rules())),
            Err(e) if has_code(&e, &["NoSuchLifecycleConfiguration"]) => None,
            Err(e) => return Err(sdk_error("s3 get_bucket_lifecycle_configuration", &e)),
        };
        match declared_change(desired, current.as_ref()) {
            Change::Keep => {}
            Change::Delete => {
                self.client
                    .delete_bucket_lifecycle()
                    .bucket(bucket)
                    .send()
                    .await
                    .map_err(|e| sdk_error("s3 delete_bucket_lifecycle", &e))?;
                sink.info("Lifecycle 設定を削除しました");
            }
            Change::Put(rules) => {
                let count = rules.as_array().map_or(0, Vec::len);
                self.client
                    .put_bucket_lifecycle_configuration()
                    .bucket(bucket)
                    .lifecycle_configuration(lifecycle_configuration(&rules)?)
                    .send()
                    .await
                    .map_err(|e| sdk_error("s3 put_bucket_lifecycle_configuration", &e))?;
                sink.info(&format!("Lifecycle ルールを適用しました: {count} 件"));
            }
        }
        Ok(())
    }
}

fn strings(value: &Json) -> Vec<String> {
    value
        .as_array()
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default()
}

/// Python の `str(list[str])` (`['a', 'b']`)
fn py_str_list(values: &[String]) -> String {
    let quoted: Vec<String> = values.iter().map(|value| format!("'{value}'")).collect();
    format!("[{}]", quoted.join(", "))
}

/// 望む CORS ルール (boto3 の形の JSON) を SDK の型にする
fn cors_configuration(rules: &Json) -> Result<CorsConfiguration> {
    let mut builder = CorsConfiguration::builder();
    for rule in rules
        .as_array()
        .context("CORS ルールが配列ではありません")?
    {
        let max_age = rule["MaxAgeSeconds"]
            .as_i64()
            .context("MaxAgeSeconds がありません")?;
        let built = CorsRule::builder()
            .set_allowed_origins(Some(strings(&rule["AllowedOrigins"])))
            .set_allowed_methods(Some(strings(&rule["AllowedMethods"])))
            .set_allowed_headers(Some(strings(&rule["AllowedHeaders"])))
            .max_age_seconds(i32::try_from(max_age)?)
            .build()
            .map_err(|e| anyhow!("CORS ルールを組めません: {e}"))?;
        builder = builder.cors_rules(built);
    }
    builder
        .build()
        .map_err(|e| anyhow!("CORS 設定を組めません: {e}"))
}

/// 望む lifecycle ルール (boto3 の形の JSON) を SDK の型にする
fn lifecycle_configuration(rules: &Json) -> Result<BucketLifecycleConfiguration> {
    let days = |value: &Json| -> Result<Option<i32>> {
        value
            .as_i64()
            .map(i32::try_from)
            .transpose()
            .map_err(Into::into)
    };
    let mut builder = BucketLifecycleConfiguration::builder();
    for rule in rules
        .as_array()
        .context("lifecycle ルールが配列ではありません")?
    {
        let mut built = LifecycleRule::builder()
            .set_id(rule["ID"].as_str().map(str::to_string))
            .status(ExpirationStatus::Enabled)
            .filter(
                LifecycleRuleFilter::builder()
                    .set_prefix(rule["Filter"]["Prefix"].as_str().map(str::to_string))
                    .build(),
            );
        if let Some(n) = days(&rule["Expiration"]["Days"])? {
            built = built.expiration(LifecycleExpiration::builder().days(n).build());
        }
        if let Some(n) = days(&rule["NoncurrentVersionExpiration"]["NoncurrentDays"])? {
            built = built.noncurrent_version_expiration(
                NoncurrentVersionExpiration::builder()
                    .noncurrent_days(n)
                    .build(),
            );
        }
        builder = builder.rules(
            built
                .build()
                .map_err(|e| anyhow!("lifecycle ルールを組めません: {e}"))?,
        );
    }
    builder
        .build()
        .map_err(|e| anyhow!("lifecycle 設定を組めません: {e}"))
}

/// まとめて消す。応答に個別の失敗があれば止める
async fn delete_batch(client: &Client, bucket: &str, objects: Vec<ObjectIdentifier>) -> Result<()> {
    if objects.is_empty() {
        return Ok(());
    }
    let delete = Delete::builder()
        .set_objects(Some(objects))
        .build()
        .map_err(|e| anyhow!("delete_objects の引数を組めません: {e}"))?;
    let response = client
        .delete_objects()
        .bucket(bucket)
        .delete(delete)
        .send()
        .await
        .map_err(|e| sdk_error("s3 delete_objects", &e))?;
    if let Some(error) = response.errors().first() {
        bail!(
            "s3://{bucket}/{} を削除できません ({}: {})。ほか {} 件",
            error.key().unwrap_or_default(),
            error.code().unwrap_or_default(),
            error.message().unwrap_or_default(),
            response.errors().len() - 1
        );
    }
    Ok(())
}

fn object_id(key: Option<&str>, version: Option<&str>) -> Result<ObjectIdentifier> {
    ObjectIdentifier::builder()
        .set_key(key.map(str::to_string))
        .set_version_id(version.map(str::to_string))
        .build()
        .map_err(|e| anyhow!("削除する object を特定できません: {e}"))
}

/// bucket の全 object (version・delete marker を含む) を消す。
///
/// 一覧の先頭のページを消しては取り直す。消しながら続きのページを marker でたどると、
/// marker にした version 自体が消えていて続きを正しく取れないことがある
pub async fn empty_bucket(client: &Client, bucket: &str) -> Result<()> {
    let mut previous: Option<String> = None;
    loop {
        let page = client
            .list_objects_v2()
            .bucket(bucket)
            .send()
            .await
            .map_err(|e| sdk_error("s3 list_objects_v2", &e))?;
        let objects = page
            .contents()
            .iter()
            .map(|object| object_id(object.key(), None))
            .collect::<Result<Vec<_>>>()?;
        ensure_progress(bucket, &mut previous, objects.first())?;
        delete_batch(client, bucket, objects).await?;
        if page.is_truncated() != Some(true) {
            break;
        }
    }
    let mut previous: Option<String> = None;
    loop {
        let page = client
            .list_object_versions()
            .bucket(bucket)
            .send()
            .await
            .map_err(|e| sdk_error("s3 list_object_versions", &e))?;
        let mut objects = Vec::new();
        for version in page.versions() {
            objects.push(object_id(version.key(), version.version_id())?);
        }
        for marker in page.delete_markers() {
            objects.push(object_id(marker.key(), marker.version_id())?);
        }
        ensure_progress(bucket, &mut previous, objects.first())?;
        delete_batch(client, bucket, objects).await?;
        if page.is_truncated() != Some(true) {
            break;
        }
    }
    Ok(())
}

/// 消したはずの object が次の一覧の先頭にまた出てきたら止める (終わらない繰り返しを防ぐ)
fn ensure_progress(
    bucket: &str,
    previous: &mut Option<String>,
    first: Option<&ObjectIdentifier>,
) -> Result<()> {
    let current = first.map(|object| {
        format!(
            "{} {}",
            object.key(),
            object.version_id().unwrap_or_default()
        )
    });
    if current.is_some() && current == *previous {
        bail!("bucket {bucket} の object を削除できません (削除しても一覧に残ります)。");
    }
    *previous = current;
    Ok(())
}

/// bucket を中身ごと消す。無ければ何もしない (消したら `true`)
pub async fn delete_bucket_with_contents(client: &Client, bucket: &str) -> Result<bool> {
    if !bucket_exists(client, bucket).await? {
        return Ok(false);
    }
    empty_bucket(client, bucket).await?;
    client
        .delete_bucket()
        .bucket(bucket)
        .send()
        .await
        .map_err(|e| sdk_error("s3 delete_bucket", &e))?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use std::cell::Cell;

    use super::*;
    use crate::stack::testing::{context, read_json, repo_root, vector_dirs};
    use crate::status::sink::{Recorded, Style};

    fn run<T>(future: impl std::future::Future<Output = T>) -> T {
        tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .expect("runtime")
            .block_on(future)
    }

    #[test]
    fn test_desired_settings_match_python() {
        let root = repo_root();
        let mut count = 0;
        for dir in vector_dirs() {
            let path = dir.join("documents.json");
            if !path.is_file() {
                continue;
            }
            let want = &read_json(&path)["s3"];
            if want.is_null() {
                continue;
            }
            let ctx = context(&root, &read_json(&dir.join("manifest.json")));
            let s3 = ctx.s3.as_ref().expect("s3");
            let mut sink = Recorded::default();
            let cors = s3_status::desired_cors(s3, &ctx.cloudfront, &mut sink);
            let label = dir.display();
            assert_eq!(cors.unwrap_or(Json::Null), want["cors_rules"], "{label}");
            assert_eq!(
                s3_status::desired_lifecycle(s3).unwrap_or(Json::Null),
                want["lifecycle_rules"],
                "{label}"
            );
            assert_eq!(
                public_access_block_should_be(),
                want["public_access_block"],
                "{label}"
            );
            assert_eq!(json!(s3.versioning), want["versioning"], "{label}");
            count += 1;
        }
        assert!(count > 10, "vector が少なすぎます: {count}");
    }

    #[test]
    fn test_declared_change() {
        let rules = json!([{"ID": "a"}]);
        assert_eq!(declared_change(None, None), Change::Keep);
        assert_eq!(
            declared_change(Some(rules.clone()), Some(&rules)),
            Change::Keep
        );
        assert_eq!(declared_change(None, Some(&rules)), Change::Delete);
        assert_eq!(
            declared_change(Some(rules.clone()), None),
            Change::Put(rules.clone())
        );
        assert_eq!(
            declared_change(Some(rules.clone()), Some(&json!([{"ID": "b"}]))),
            Change::Put(rules)
        );
    }

    #[test]
    fn test_versioning_change() {
        assert_eq!(versioning_change(true, None), Some("Enabled"));
        assert_eq!(versioning_change(true, Some("Suspended")), Some("Enabled"));
        assert_eq!(versioning_change(true, Some("Enabled")), None);
        assert_eq!(versioning_change(false, Some("Enabled")), Some("Suspended"));
        assert_eq!(versioning_change(false, Some("Suspended")), None);
        assert_eq!(versioning_change(false, None), None);
    }

    #[test]
    fn test_settings_convert_to_sdk_types() {
        let cors = cors_configuration(&json!([{
            "AllowedOrigins": ["https://a.example"],
            "AllowedMethods": ["GET", "PUT"],
            "AllowedHeaders": ["*"],
            "MaxAgeSeconds": 3600,
        }]))
        .expect("cors");
        let rule = &cors.cors_rules()[0];
        assert_eq!(rule.allowed_origins(), ["https://a.example"]);
        assert_eq!(rule.allowed_methods(), ["GET", "PUT"]);
        assert_eq!(rule.max_age_seconds(), Some(3600));
        // 書いた値を読み戻すと、望む値と同じ JSON になる (status が COMPLETED と判定する)
        let rules = json!([
            {"ID": "tmp", "Status": "Enabled", "Filter": {"Prefix": "tmp/"},
             "Expiration": {"Days": 7}},
            {"ID": "old", "Status": "Enabled", "Filter": {"Prefix": ""},
             "NoncurrentVersionExpiration": {"NoncurrentDays": 30}},
        ]);
        let lifecycle = lifecycle_configuration(&rules).expect("lifecycle");
        assert_eq!(s3_status::lifecycle_rules_json(lifecycle.rules()), rules);
    }

    const FAST: CreateRetry = CreateRetry {
        interval: Duration::ZERO,
        attempts: 3,
    };

    #[test]
    fn test_create_retries_while_operation_aborted() {
        let calls = Cell::new(0);
        let mut sink = Recorded::default();
        run(create_with_retry("b", FAST, &mut sink, || async {
            calls.set(calls.get() + 1);
            if calls.get() < 3 {
                Err(CreateError::Aborted)
            } else {
                Ok(())
            }
        }))
        .expect("3 回目で成功");
        assert_eq!(calls.get(), 3);
        let warnings: Vec<_> = sink.0.iter().filter(|r| r.1 == Style::Warning).collect();
        assert_eq!(warnings.len(), 2);
    }

    #[test]
    fn test_create_gives_up_after_attempts() {
        let calls = Cell::new(0);
        let mut sink = Recorded::default();
        let error = run(create_with_retry("b", FAST, &mut sink, || async {
            calls.set(calls.get() + 1);
            Err(CreateError::Aborted)
        }))
        .expect_err("打ち切り");
        assert_eq!(calls.get(), 4);
        assert!(error.to_string().contains("OperationAborted のまま"));
    }

    #[test]
    fn test_create_does_not_retry_other_errors() {
        let calls = Cell::new(0);
        let mut sink = Recorded::default();
        let error = run(create_with_retry("b", FAST, &mut sink, || async {
            calls.set(calls.get() + 1);
            Err(CreateError::Other(anyhow!("BucketAlreadyExists")))
        }))
        .expect_err("即中止");
        assert_eq!(calls.get(), 1);
        assert_eq!(error.to_string(), "BucketAlreadyExists");
        assert!(sink.0.is_empty());
    }

    #[test]
    fn test_default_retry_waits_an_hour() {
        let retry = CreateRetry::default();
        assert_eq!(retry.minutes(retry.attempts), 60);
    }

    #[test]
    fn test_py_str_list() {
        let values = [
            "https://a".to_string(),
            "https://*.cloudfront.net".to_string(),
        ];
        assert_eq!(
            py_str_list(&values),
            "['https://a', 'https://*.cloudfront.net']"
        );
    }
}
