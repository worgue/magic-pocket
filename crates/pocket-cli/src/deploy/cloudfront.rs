//! CloudFront の stack と、stack の外に持つ状態 (bucket policy・KVS) の手順。Python 版
//! `pocket_cli/resources/cloudfront.py` の update / ensure_post_deploy_state / delete。
//!
//! bucket policy は distribution の id が決まってからでないと書けず、KVS の値
//! (token_secret / basic 認証) は template に入れないので、どちらも stack の完了後に書く。
//! AWS の呼び出しは [`Stacks`] / [`CloudFrontAws`] / [`SecretStore`] の裏に置く。

use anyhow::{anyhow, Context as _, Result};
use aws_config::SdkConfig;
use magic_pocket_settings::context::{CloudFrontContext, Context};
use magic_pocket_settings::pyjson;
use serde_json::{json, Value as Json};

use super::edge::{find_cloudfront, EDGE_REGION};
use super::s3;
use super::secret_store::SecretStore;
use super::stacks::{self, Stacks};
use super::template_secrets;
use crate::aws_util::{has_code, regional_config, sdk_error};
use crate::stack::cloudfront::{self as cf_stack, CloudFrontInputs};
use crate::stack::ops::{
    cfn_status, stack_outputs, stack_status, template_synced, ResourceStatus, WaitOptions,
};
use crate::stack::{StackOutputs, StackTemplate};
use crate::status::sink::Sink;

const POLICY_VERSION: &str = "2012-10-17";

/// stack の外の状態に対する AWS の操作
pub trait CloudFrontAws {
    async fn account_id(&self) -> Result<String>;
    /// bucket policy。設定されていなければ `None`
    async fn bucket_policy(&self, region: &str, bucket: &str) -> Result<Option<Json>>;
    async fn put_bucket_policy(&self, region: &str, bucket: &str, policy: &str) -> Result<()>;
    async fn delete_bucket_policy(&self, region: &str, bucket: &str) -> Result<()>;
    /// bucket を中身ごと消す (無ければ何もしない)
    async fn delete_bucket_with_contents(&self, region: &str, bucket: &str) -> Result<()>;
    /// KVS の今の ETag (書き込みの条件に使う)
    async fn kvs_etag(&self, region: &str, kvs_arn: &str) -> Result<String>;
    async fn kvs_put_key(
        &self,
        region: &str,
        kvs_arn: &str,
        key: &str,
        value: &str,
        etag: &str,
    ) -> Result<()>;
}

/// bucket policy に対して行う操作
#[derive(Debug, Clone, PartialEq)]
pub enum PolicyChange {
    Keep,
    Put(Json),
    Delete,
}

/// distribution に bucket の読み取りを許す statement
/// (Python の `bucket_policy_statement_should_contain`)
pub fn bucket_policy_statement(
    cf: &CloudFrontContext,
    account_id: &str,
    distribution_id: &str,
) -> Json {
    json!({
        "Sid": format!("AllowCloudFrontReadOnly{}", cf.yaml_key),
        "Effect": "Allow",
        "Principal": {"Service": "cloudfront.amazonaws.com"},
        "Action": "s3:GetObject",
        "Resource": format!("arn:aws:s3:::{}{}/*", cf.bucket_name, cf.bucket_policy_prefix),
        "Condition": {
            "StringEquals": {
                "AWS:SourceArn":
                    format!("arn:aws:cloudfront::{account_id}:distribution/{distribution_id}")
            }
        },
    })
}

fn statements(policy: &Json) -> Result<&Vec<Json>> {
    policy["Statement"]
        .as_array()
        .context("bucket policy の Statement が配列ではありません")
}

fn with_statement(policy: &Json, own: &Json) -> Result<Json> {
    let mut updated = policy.clone();
    updated["Statement"]
        .as_array_mut()
        .context("bucket policy の Statement が配列ではありません")?
        .push(own.clone());
    Ok(updated)
}

/// 自分の statement が policy にある状態にする。statement は全体の一致で探す
/// (Sid が同じでも中身が違えば別物として足す)。戻り値の 2 つ目は警告
pub fn ensure_policy(current: Option<&Json>, own: &Json) -> Result<(PolicyChange, Option<String>)> {
    let Some(current) = current else {
        let policy = json!({"Version": POLICY_VERSION, "Statement": [own]});
        return Ok((PolicyChange::Put(policy), None));
    };
    let contains = statements(current)?.contains(own);
    if current["Version"] != POLICY_VERSION {
        let warning = format!(
            "Bucket policy version {} will be upgraded to {POLICY_VERSION}.",
            current["Version"].as_str().unwrap_or_default()
        );
        let mut updated = if contains {
            current.clone()
        } else {
            with_statement(current, own)?
        };
        updated["Version"] = json!(POLICY_VERSION);
        return Ok((PolicyChange::Put(updated), Some(warning)));
    }
    if contains {
        return Ok((PolicyChange::Keep, None));
    }
    Ok((PolicyChange::Put(with_statement(current, own)?), None))
}

/// 自分の statement を policy から外す。他の statement が無くなれば policy ごと消す。
/// 手を出さない場合 (Version が違う・statement が無い) は警告だけ返す
pub fn remove_policy(current: Option<&Json>, own: &Json) -> Result<(PolicyChange, Option<String>)> {
    let Some(current) = current else {
        return Ok((PolicyChange::Keep, None));
    };
    if current["Version"] != POLICY_VERSION {
        let warning = "Bucket policy version missmatch. Check the policy manually.";
        return Ok((PolicyChange::Keep, Some(warning.to_string())));
    }
    let existing = statements(current)?;
    let Some(position) = existing.iter().position(|statement| statement == own) else {
        let warning = "No policy found. Check the policy manually.";
        return Ok((PolicyChange::Keep, Some(warning.to_string())));
    };
    if existing.len() == 1 {
        return Ok((PolicyChange::Delete, None));
    }
    let mut updated = current.clone();
    if let Some(list) = updated["Statement"].as_array_mut() {
        list.remove(position);
    }
    Ok((PolicyChange::Put(updated), None))
}

/// stack の完了待ち
#[derive(Debug, Clone, Copy)]
pub struct CloudFrontWaits {
    /// distribution の作成・更新は数分かかる (Python 版は 1800 秒を 10 秒おき)
    pub stack: WaitOptions,
    pub delete: WaitOptions,
}

impl Default for CloudFrontWaits {
    fn default() -> Self {
        Self {
            stack: WaitOptions::new(1800, 10),
            delete: WaitOptions::new(900, 15),
        }
    }
}

pub struct CloudFrontDeploy<'a, K, A, S> {
    pub stacks: &'a K,
    pub aws: &'a A,
    pub store: &'a S,
    pub waits: CloudFrontWaits,
}

/// template と、stack の完了後に KVS へ書く値
struct Prepared {
    stack: StackTemplate,
    /// (KVS の key, 値)。値があるものだけ
    kvs: Vec<(&'static str, String)>,
}

impl<K: Stacks, A: CloudFrontAws, S: SecretStore> CloudFrontDeploy<'_, K, A, S> {
    /// template に入る値と KVS に書く値を store から読む (書き込みはしない)
    async fn prepare(
        &self,
        ctx: &Context,
        cf: &CloudFrontContext,
        sink: &mut dyn Sink,
    ) -> Result<Prepared> {
        let mut kvs = Vec::new();
        if let Some(key) = cf.token_secret.as_deref().filter(|key| !key.is_empty()) {
            if let Some(view) = template_secrets::managed_secret_view(ctx, key) {
                let secrets = self.store.read(view).await?;
                if let Some(value) = template_secrets::token_secret_value(key, &secrets, sink) {
                    kvs.push(("token_secret", value));
                }
            }
        }
        if let Some(key) = cf.basic_auth.as_deref().filter(|key| !key.is_empty()) {
            if let Some(view) = template_secrets::managed_secret_view(ctx, key) {
                let secrets = self.store.read(view).await?;
                if let Some(value) = template_secrets::basic_auth_expected(key, &secrets, sink) {
                    kvs.push(("basic_auth", value));
                }
            }
        }
        kvs.retain(|(_, value)| !value.is_empty());
        let origin_verify_secret = match ctx.secrets.as_ref() {
            Some(view) if cf.enable_origin_verify => {
                template_secrets::origin_verify_secret(&self.store.read(view).await?, sink)
            }
            _ => String::new(),
        };
        let acm_output = match cf.domain.as_deref().filter(|domain| !domain.is_empty()) {
            Some(_) => {
                self.outputs(EDGE_REGION, &cf_stack::acm_stack_name(cf))
                    .await?
            }
            None => None,
        };
        let waf_output = match cf.waf.as_ref() {
            Some(_) => {
                self.outputs(EDGE_REGION, &cf_stack::waf_stack_name(cf))
                    .await?
            }
            None => None,
        };
        let inputs = CloudFrontInputs {
            acm_output,
            waf_output,
            origin_verify_secret,
        };
        Ok(Prepared {
            stack: cf_stack::cloudfront_stack(cf, &inputs)?,
            kvs,
        })
    }

    /// cloudfront の stack の template (store と他の stack から値を読んで組む)
    pub async fn template(
        &self,
        ctx: &Context,
        name: &str,
        sink: &mut dyn Sink,
    ) -> Result<StackTemplate> {
        let cf = find_cloudfront(ctx, name)?;
        Ok(self.prepare(ctx, cf, sink).await?.stack)
    }

    async fn outputs(&self, region: &str, name: &str) -> Result<Option<StackOutputs>> {
        let description = self.stacks.describe(region, name).await?;
        Ok(stack_outputs(description.as_ref()))
    }

    /// stack を作る (create と update は同じ手順)。完了を待ってから bucket policy と
    /// KVS を書く
    pub async fn update(&self, ctx: &Context, name: &str, sink: &mut dyn Sink) -> Result<()> {
        let cf = find_cloudfront(ctx, name)?;
        let prepared = self.prepare(ctx, cf, sink).await?;
        let stack = &prepared.stack;
        let description = self.stacks.describe(&stack.region, &stack.name).await?;
        if cfn_status(description.as_ref())? == ResourceStatus::NoExist {
            self.stacks.create(stack).await?;
        } else if !template_synced(description.as_ref(), stack)? {
            self.stacks.update(stack).await?;
        }
        sink.log("Waiting for cloudformation stack to be completed ...");
        sink.log("This may take a few minutes.");
        sink.log("Because cloudfront distribution id is required to set s3 bucket policy.");
        sink.info("If you want to exit, you can safely kill this process.");
        sink.info("In that case, run `pocket resource cloudfront update` later.");
        stacks::wait(
            self.stacks,
            &stack.region,
            &stack.name,
            "cloudfront",
            ResourceStatus::Completed,
            self.waits.stack,
        )
        .await?;
        self.cleanup_legacy_redirect_from(cf, sink).await;
        self.ensure_bucket_policy(cf, sink).await?;
        self.write_kvs(cf, &prepared.kvs, sink).await?;
        sink.log("Bucket for cloudfront is ready.");
        warn_contents(cf, sink);
        Ok(())
    }

    /// stack の外の状態 (bucket policy / KVS) を合わせ直す。
    ///
    /// update の完了待ちが timeout すると、stack はその後に完了しても次の deploy では
    /// 「変更なし」と判定され update が呼ばれない。deploy の最後に毎回これを呼んで補う
    pub async fn ensure_post_deploy_state(
        &self,
        ctx: &Context,
        name: &str,
        sink: &mut dyn Sink,
    ) -> Result<()> {
        let cf = find_cloudfront(ctx, name)?;
        let prepared = self.prepare(ctx, cf, sink).await?;
        let stack = &prepared.stack;
        let description = self.stacks.describe(&stack.region, &stack.name).await?;
        if stack_status(description.as_ref(), stack)? != ResourceStatus::Completed {
            return Ok(());
        }
        self.ensure_bucket_policy(cf, sink).await?;
        self.write_kvs(cf, &prepared.kvs, sink).await
    }

    /// bucket policy から自分の statement を外し、stack を消す (bucket は消さない)
    pub async fn delete(&self, cf: &CloudFrontContext, sink: &mut dyn Sink) -> Result<()> {
        self.cleanup_legacy_redirect_from(cf, sink).await;
        self.delete_bucket_policy(cf, sink).await?;
        sink.info("Deleting cloudformation stack for cloudfront ...");
        let name = cf_stack::cloudfront_stack_name(cf);
        self.stacks.delete(&cf.region, &name).await?;
        stacks::wait(
            self.stacks,
            &cf.region,
            &name,
            "cloudfront",
            ResourceStatus::NoExist,
            self.waits.delete,
        )
        .await?;
        sink.warning(&format!(
            "S3 bucket is managed by the S3 resource: {}",
            cf.bucket_name
        ));
        Ok(())
    }

    /// 旧 redirect_from の実装が作っていた S3 website bucket (名前 = redirect 元の
    /// ドメイン) を消す。消せなくても deploy は止めない (今の 301 は bucket を使わない)
    async fn cleanup_legacy_redirect_from(&self, cf: &CloudFrontContext, sink: &mut dyn Sink) {
        for redirect in &cf.redirect_from {
            let result = self
                .aws
                .delete_bucket_with_contents(&cf.s3_region, &redirect.domain)
                .await;
            if result.is_err() {
                sink.warning(&format!(
                    "Legacy redirect-from bucket を削除できませんでした \
                     (別アカウント所有 / リージョン不一致の可能性)。\
                     不要なら手動で確認・削除してください: {}",
                    redirect.domain
                ));
            }
        }
    }

    /// stack の output にある distribution の id
    pub async fn distribution_id(&self, cf: &CloudFrontContext) -> Result<String> {
        let outputs = self
            .outputs(&cf.region, &cf_stack::cloudfront_stack_name(cf))
            .await?
            .context("Cloudfront distribution is not created yet.")?;
        outputs
            .get("DistributionId")
            .cloned()
            .context("Cloudfront の stack に DistributionId の output がありません。")
    }

    async fn own_statement(&self, cf: &CloudFrontContext) -> Result<Json> {
        let account_id = self.aws.account_id().await?;
        let distribution_id = self.distribution_id(cf).await?;
        Ok(bucket_policy_statement(cf, &account_id, &distribution_id))
    }

    async fn apply_policy(
        &self,
        cf: &CloudFrontContext,
        current: Option<&Json>,
        change: PolicyChange,
        sink: &mut dyn Sink,
    ) -> Result<()> {
        let bucket = &cf.bucket_name;
        let show = |policy: Option<&Json>| policy.map_or("None".to_string(), Json::to_string);
        match change {
            PolicyChange::Keep => {}
            PolicyChange::Delete => {
                sink.info(&format!("Deleting bucket policy for {bucket}."));
                sink.log(&format!("Current policy: {}", show(current)));
                self.aws.delete_bucket_policy(&cf.s3_region, bucket).await?;
                sink.log("Updated policy: None");
            }
            PolicyChange::Put(policy) => {
                sink.info(&format!("Updating bucket policy for {bucket}."));
                sink.log(&format!("Current policy: {}", show(current)));
                self.aws
                    .put_bucket_policy(&cf.s3_region, bucket, &pyjson::dumps(&policy))
                    .await?;
                sink.log(&format!("Updated policy: {policy}"));
            }
        }
        Ok(())
    }

    async fn ensure_bucket_policy(
        &self,
        cf: &CloudFrontContext,
        sink: &mut dyn Sink,
    ) -> Result<()> {
        let current = self
            .aws
            .bucket_policy(&cf.s3_region, &cf.bucket_name)
            .await?;
        let own = self.own_statement(cf).await?;
        let (change, warning) = ensure_policy(current.as_ref(), &own)?;
        if let Some(warning) = warning {
            sink.warning(&warning);
        }
        if change == PolicyChange::Keep {
            sink.info("Bucket policy is already configured properly.");
        }
        self.apply_policy(cf, current.as_ref(), change, sink).await
    }

    async fn delete_bucket_policy(
        &self,
        cf: &CloudFrontContext,
        sink: &mut dyn Sink,
    ) -> Result<()> {
        let own = self.own_statement(cf).await?;
        let current = self
            .aws
            .bucket_policy(&cf.s3_region, &cf.bucket_name)
            .await?;
        if current.is_none() {
            sink.info("Bucket policy is already None.");
        }
        let (change, warning) = remove_policy(current.as_ref(), &own)?;
        if let Some(warning) = warning {
            sink.warning(&warning);
        }
        self.apply_policy(cf, current.as_ref(), change, sink).await
    }

    /// KVS に値を書く。key ごとに ETag を取り直す (書くたびに変わる)。
    /// 設定から外した key は消さない
    async fn write_kvs(
        &self,
        cf: &CloudFrontContext,
        entries: &[(&'static str, String)],
        sink: &mut dyn Sink,
    ) -> Result<()> {
        if entries.is_empty() {
            return Ok(());
        }
        let Some(outputs) = self
            .outputs(&cf.region, &cf_stack::cloudfront_stack_name(cf))
            .await?
        else {
            sink.warning("スタック出力が取得できません。KVS 書き込みをスキップします。");
            return Ok(());
        };
        let Some(kvs_arn) = outputs.get("TokenKvsArn").filter(|arn| !arn.is_empty()) else {
            sink.warning("TokenKvsArn が出力に見つかりません。");
            return Ok(());
        };
        for (key, value) in entries {
            let etag = self.aws.kvs_etag(&cf.region, kvs_arn).await?;
            self.aws
                .kvs_put_key(&cf.region, kvs_arn, key, value, &etag)
                .await?;
            sink.info(&format!("KVS に {key} を書き込みました"));
        }
        Ok(())
    }
}

/// pocket が upload しない route の、手動 upload の案内 (Python の `warn_contents`)
pub fn warn_contents(cf: &CloudFrontContext, sink: &mut dyn Sink) {
    let bucket = &cf.bucket_name;
    for route in &cf.routes {
        if route
            .upload_dir
            .as_deref()
            .is_some_and(|dir| !dir.is_empty())
        {
            continue;
        }
        let label = if route.path_pattern.is_empty() {
            "default"
        } else {
            &route.path_pattern
        };
        if route.is_object_hash {
            sink.info(&format!(
                "{label} は object_hash route です。\
                 `pocket static publish --stage {}` で上げてください \
                 (deploy は触りません)",
                cf.stage
            ));
            continue;
        }
        let origin = format!("{}{}", route.origin_path, route.path_pattern);
        sink.warning(&format!("Upload files manually to s3://{bucket}{origin}"));
        if route.is_spa {
            sink.info(&format!("{label} is a spa route."));
            sink.info("Set proper cahce headers.");
            sink.info(&format!(
                "e.g) npx s3-spa-upload build {bucket} --delete --prefix {}",
                origin.get(1..).unwrap_or_default()
            ));
        } else if route.versioning.is_some() {
            sink.info("This is a versioned route.");
            sink.info("Just upload your files. CloudFront will set cache-control headers.");
            sink.info(&format!("e.g) aws s3 sync data s3://{bucket}{origin}"));
        }
    }
}

/// AWS に対する [`CloudFrontAws`]
pub struct AwsCloudFront<'a> {
    pub sdk: &'a SdkConfig,
}

impl AwsCloudFront<'_> {
    fn s3(&self, region: &str) -> aws_sdk_s3::Client {
        aws_sdk_s3::Client::new(&regional_config(self.sdk, region))
    }

    fn kvs(&self, region: &str) -> aws_sdk_cloudfrontkeyvaluestore::Client {
        aws_sdk_cloudfrontkeyvaluestore::Client::new(&regional_config(self.sdk, region))
    }
}

impl CloudFrontAws for AwsCloudFront<'_> {
    async fn account_id(&self) -> Result<String> {
        let identity = aws_sdk_sts::Client::new(self.sdk)
            .get_caller_identity()
            .send()
            .await
            .map_err(|e| sdk_error("sts get_caller_identity", &e))?;
        identity
            .account()
            .map(str::to_string)
            .context("AWS account id を取得できません")
    }

    async fn bucket_policy(&self, region: &str, bucket: &str) -> Result<Option<Json>> {
        // 「policy が無い」だけを無しとする。権限不足などを無しと見なすと、読めなかった
        // policy を新しい policy で上書きしてしまう
        match self
            .s3(region)
            .get_bucket_policy()
            .bucket(bucket)
            .send()
            .await
        {
            Ok(response) => {
                let text = response.policy().unwrap_or_default();
                let policy = serde_json::from_str(text)
                    .map_err(|e| anyhow!("bucket {bucket} の policy を読めません: {e}"))?;
                Ok(Some(policy))
            }
            Err(e) if has_code(&e, &["NoSuchBucketPolicy"]) => Ok(None),
            Err(e) => Err(sdk_error("s3 get_bucket_policy", &e)),
        }
    }

    async fn put_bucket_policy(&self, region: &str, bucket: &str, policy: &str) -> Result<()> {
        self.s3(region)
            .put_bucket_policy()
            .bucket(bucket)
            .policy(policy)
            .send()
            .await
            .map_err(|e| sdk_error("s3 put_bucket_policy", &e))?;
        Ok(())
    }

    async fn delete_bucket_policy(&self, region: &str, bucket: &str) -> Result<()> {
        self.s3(region)
            .delete_bucket_policy()
            .bucket(bucket)
            .send()
            .await
            .map_err(|e| sdk_error("s3 delete_bucket_policy", &e))?;
        Ok(())
    }

    async fn delete_bucket_with_contents(&self, region: &str, bucket: &str) -> Result<()> {
        s3::delete_bucket_with_contents(&self.s3(region), bucket).await?;
        Ok(())
    }

    async fn kvs_etag(&self, region: &str, kvs_arn: &str) -> Result<String> {
        let described = self
            .kvs(region)
            .describe_key_value_store()
            .kvs_arn(kvs_arn)
            .send()
            .await
            .map_err(|e| sdk_error("cloudfront-keyvaluestore describe_key_value_store", &e))?;
        Ok(described.e_tag().to_string())
    }

    async fn kvs_put_key(
        &self,
        region: &str,
        kvs_arn: &str,
        key: &str,
        value: &str,
        etag: &str,
    ) -> Result<()> {
        self.kvs(region)
            .put_key()
            .kvs_arn(kvs_arn)
            .key(key)
            .value(value)
            .if_match(etag)
            .send()
            .await
            .map_err(|e| sdk_error("cloudfront-keyvaluestore put_key", &e))?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;
    use std::collections::BTreeMap;
    use std::time::Duration;

    use anyhow::bail;

    use super::*;
    use crate::deploy::stacks::fake::FakeStacks;
    use crate::deploy::testing::FakeStore;
    use crate::stack::testing::{
        context, read_json, repo_root, vector_context, vector_dirs, vectors_dir,
    };
    use crate::status::sink::{Recorded, Style};

    const VECTOR: &str = "toml_vectors/cloudfront_full.dev";
    const ACCOUNT: &str = "123456789012";
    const DISTRIBUTION: &str = "EDIST";
    const KVS_ARN: &str = "arn:aws:cloudfront::123456789012:key-value-store/kvs";

    fn run<T>(future: impl std::future::Future<Output = T>) -> T {
        tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .expect("runtime")
            .block_on(future)
    }

    #[derive(Default)]
    struct FakeAws {
        /// bucket → policy
        policies: RefCell<BTreeMap<String, Json>>,
        /// 消せない bucket
        undeletable: Vec<String>,
        kvs_fails: bool,
        etag: RefCell<u32>,
        calls: RefCell<Vec<String>>,
    }

    impl FakeAws {
        fn calls(&self) -> Vec<String> {
            self.calls.borrow().clone()
        }

        fn record(&self, call: String) {
            self.calls.borrow_mut().push(call);
        }
    }

    impl CloudFrontAws for FakeAws {
        async fn account_id(&self) -> Result<String> {
            Ok(ACCOUNT.to_string())
        }

        async fn bucket_policy(&self, _region: &str, bucket: &str) -> Result<Option<Json>> {
            Ok(self.policies.borrow().get(bucket).cloned())
        }

        async fn put_bucket_policy(&self, region: &str, bucket: &str, policy: &str) -> Result<()> {
            self.record(format!("put_policy {region} {bucket}"));
            let parsed = serde_json::from_str(policy).expect("JSON");
            self.policies
                .borrow_mut()
                .insert(bucket.to_string(), parsed);
            Ok(())
        }

        async fn delete_bucket_policy(&self, region: &str, bucket: &str) -> Result<()> {
            self.record(format!("delete_policy {region} {bucket}"));
            self.policies.borrow_mut().remove(bucket);
            Ok(())
        }

        async fn delete_bucket_with_contents(&self, region: &str, bucket: &str) -> Result<()> {
            self.record(format!("delete_bucket {region} {bucket}"));
            if self.undeletable.iter().any(|name| name == bucket) {
                bail!("AccessDenied");
            }
            Ok(())
        }

        async fn kvs_etag(&self, _region: &str, kvs_arn: &str) -> Result<String> {
            self.record(format!("kvs_etag {kvs_arn}"));
            Ok(format!("E{}", self.etag.borrow()))
        }

        async fn kvs_put_key(
            &self,
            region: &str,
            _kvs_arn: &str,
            key: &str,
            value: &str,
            etag: &str,
        ) -> Result<()> {
            if self.kvs_fails {
                bail!("PreconditionFailed");
            }
            self.record(format!("kvs_put {region} {key}={value} if-match {etag}"));
            *self.etag.borrow_mut() += 1;
            Ok(())
        }
    }

    fn fast() -> CloudFrontWaits {
        let wait = WaitOptions {
            attempts: 3,
            interval: Duration::ZERO,
            timeout_secs: 0,
        };
        CloudFrontWaits {
            stack: wait,
            delete: wait,
        }
    }

    struct World {
        ctx: Context,
        stacks: FakeStacks,
        aws: FakeAws,
        store: FakeStore,
    }

    impl World {
        /// token_secret / basic 認証 / origin verify の secret が store にある状態
        fn new() -> Self {
            let ctx = vector_context(VECTOR);
            let mut store = FakeStore::default();
            let view = template_secrets::managed_secret_view(&ctx, "TOKEN_SECRET").expect("view");
            store = store.with(
                &view.pocket_key,
                json!({"TOKEN_SECRET": "tok", "BASIC_AUTH": "admin:pw"}),
            );
            let shared = ctx.secrets.as_ref().expect("shared");
            store = store.with(
                &shared.pocket_key,
                json!({template_secrets::ORIGIN_VERIFY_SECRET_KEY: "verify"}),
            );
            let stacks = FakeStacks::default();
            // template が参照する ACM / WAF の stack は deploy 済み (output は vector の固定値)
            let web = find_cloudfront(&ctx, "web").expect("web");
            for name in [cf_stack::acm_stack_name(web), cf_stack::waf_stack_name(web)] {
                let outputs = edge_outputs(&name).expect("output");
                let pairs: Vec<(&str, &str)> = outputs
                    .iter()
                    .map(|(key, value)| (key.as_str(), value.as_str()))
                    .collect();
                stacks.put(
                    &name,
                    FakeStacks::description("CREATE_COMPLETE", "x", &pairs),
                );
            }
            let name = cf_stack::cloudfront_stack_name(web);
            stacks.outputs.borrow_mut().insert(
                name,
                vec![
                    ("DistributionId".to_string(), DISTRIBUTION.to_string()),
                    ("TokenKvsArn".to_string(), KVS_ARN.to_string()),
                ],
            );
            Self {
                ctx,
                stacks,
                aws: FakeAws::default(),
                store,
            }
        }

        fn deploy(&self) -> CloudFrontDeploy<'_, FakeStacks, FakeAws, FakeStore> {
            CloudFrontDeploy {
                stacks: &self.stacks,
                aws: &self.aws,
                store: &self.store,
                waits: fast(),
            }
        }

        fn cf(&self) -> &CloudFrontContext {
            find_cloudfront(&self.ctx, "web").expect("web")
        }

        fn stack_name(&self) -> String {
            cf_stack::cloudfront_stack_name(self.cf())
        }

        fn own(&self) -> Json {
            bucket_policy_statement(self.cf(), ACCOUNT, DISTRIBUTION)
        }
    }

    /// vector に同梱された、他の stack の output
    fn edge_outputs(stack_name: &str) -> Option<StackOutputs> {
        let manifest = read_json(&vectors_dir().join(VECTOR).join("manifest.json"));
        let outputs = manifest["stack_external"]["stack_outputs"][stack_name].as_object()?;
        Some(
            outputs
                .iter()
                .map(|(key, value)| (key.clone(), value.as_str().expect("文字列").to_string()))
                .collect(),
        )
    }

    #[test]
    fn test_policy_documents_match_python() {
        let root = repo_root();
        let mut cases = 0;
        for dir in vector_dirs() {
            let path = dir.join("documents.json");
            if !path.is_file() {
                continue;
            }
            let documents = read_json(&path);
            let ctx = context(&root, &read_json(&dir.join("manifest.json")));
            let account = documents["account_id"].as_str().expect("account");
            let distribution = documents["distribution_id"].as_str().expect("distribution");
            for (name, want) in documents["cloudfront"].as_object().expect("cloudfront") {
                let cf = ctx.cloudfront.get(name).expect("cloudfront");
                let own = bucket_policy_statement(cf, account, distribution);
                let label = format!("{} {name}", dir.display());
                assert_eq!(own, want["bucket_policy_statement"], "{label}");
                for case in want["bucket_policy_cases"].as_array().expect("cases") {
                    let label = format!("{label} {}", case["name"]);
                    let current = Some(&case["current"]).filter(|c| !c.is_null());
                    let (ensured, _) = ensure_policy(current, &own).expect("ensure");
                    assert_eq!(change_json(&ensured), case["ensure"], "{label} ensure");
                    let (removed, _) = remove_policy(current, &own).expect("remove");
                    assert_eq!(change_json(&removed), case["delete"], "{label} delete");
                    cases += 1;
                }
            }
        }
        assert!(cases > 100, "vector が少なすぎます: {cases}");
    }

    fn change_json(change: &PolicyChange) -> Json {
        match change {
            PolicyChange::Keep => json!({"action": "none"}),
            PolicyChange::Delete => json!({"action": "delete"}),
            PolicyChange::Put(policy) => json!({"action": "put", "policy": policy}),
        }
    }

    #[test]
    fn test_policy_warnings() {
        let own = json!({"Sid": "own"});
        let old = json!({"Version": "2008-10-17", "Statement": [own]});
        let (_, warning) = ensure_policy(Some(&old), &own).expect("ensure");
        assert_eq!(
            warning.as_deref(),
            Some("Bucket policy version 2008-10-17 will be upgraded to 2012-10-17.")
        );
        let (change, warning) = remove_policy(Some(&old), &own).expect("remove");
        assert_eq!(change, PolicyChange::Keep);
        assert!(warning.is_some_and(|w| w.contains("version missmatch")));
        let other = json!({"Version": POLICY_VERSION, "Statement": [{"Sid": "other"}]});
        let (change, warning) = remove_policy(Some(&other), &own).expect("remove");
        assert_eq!(change, PolicyChange::Keep);
        assert!(warning.is_some_and(|w| w.contains("No policy found")));
    }

    #[test]
    fn test_update_creates_stack_then_writes_policy_and_kvs() {
        let world = World::new();
        let mut sink = Recorded::default();
        run(world.deploy().update(&world.ctx, "web", &mut sink)).expect("update");
        let cf = world.cf();
        assert_eq!(
            world.stacks.writes(),
            [format!("create {} {}", cf.region, world.stack_name())]
        );
        // stack の完了後: 旧 redirect bucket の掃除 → bucket policy → KVS (key ごとに ETag)
        assert_eq!(
            world.aws.calls(),
            [
                format!("delete_bucket {} example.com", cf.s3_region),
                format!("delete_bucket {} old.example.com", cf.s3_region),
                format!("put_policy {} {}", cf.s3_region, cf.bucket_name),
                format!("kvs_etag {KVS_ARN}"),
                format!("kvs_put {} token_secret=tok if-match E0", cf.region),
                format!("kvs_etag {KVS_ARN}"),
                format!(
                    "kvs_put {} basic_auth=Basic YWRtaW46cHc= if-match E1",
                    cf.region
                ),
            ]
        );
        let policy = world.aws.policies.borrow()[&cf.bucket_name].clone();
        assert_eq!(
            policy,
            json!({"Version": POLICY_VERSION, "Statement": [world.own()]})
        );
        // origin verify の secret は template に入る
        let description = run(world.stacks.describe(&cf.region, &world.stack_name()))
            .expect("describe")
            .expect("stack");
        let inputs = CloudFrontInputs {
            acm_output: edge_outputs(&cf_stack::acm_stack_name(cf)),
            waf_output: edge_outputs(&cf_stack::waf_stack_name(cf)),
            origin_verify_secret: "verify".to_string(),
        };
        let want = cf_stack::cloudfront_stack(cf, &inputs).expect("stack");
        assert!(template_synced(Some(&description), &want).expect("synced"));
    }

    #[test]
    fn test_update_skips_stack_write_when_synced_and_keeps_policy() {
        let world = World::new();
        let mut sink = Recorded::default();
        run(world.deploy().update(&world.ctx, "web", &mut sink)).expect("1 回目");
        let before = world.aws.calls().len();
        let mut sink = Recorded::default();
        run(world.deploy().update(&world.ctx, "web", &mut sink)).expect("2 回目");
        assert_eq!(
            world.stacks.writes().len(),
            1,
            "template が同じなら更新しない"
        );
        let calls = world.aws.calls()[before..].to_vec();
        assert!(!calls.iter().any(|call| call.starts_with("put_policy")));
        assert!(sink
            .0
            .iter()
            .any(|r| r.2 == "Bucket policy is already configured properly."));
    }

    #[test]
    fn test_update_stops_before_post_state_when_stack_fails() {
        let world = World::new();
        *world.stacks.after_write.borrow_mut() = Some("CREATE_FAILED".to_string());
        let mut sink = Recorded::default();
        run(world.deploy().update(&world.ctx, "web", &mut sink)).expect_err("失敗");
        assert!(
            world.aws.calls().is_empty(),
            "stack が完了しなければ何も書かない"
        );
    }

    #[test]
    fn test_update_continues_when_legacy_bucket_cannot_be_deleted() {
        let mut world = World::new();
        world.aws.undeletable = vec!["example.com".to_string()];
        let mut sink = Recorded::default();
        run(world.deploy().update(&world.ctx, "web", &mut sink)).expect("update");
        let warnings: Vec<_> = sink
            .0
            .iter()
            .filter(|r| r.1 == Style::Warning && r.2.contains("Legacy redirect-from bucket"))
            .collect();
        assert_eq!(warnings.len(), 1);
        assert!(warnings[0].2.ends_with(": example.com"));
        assert!(world
            .aws
            .calls()
            .iter()
            .any(|c| c.starts_with("put_policy")));
    }

    #[test]
    fn test_post_deploy_state_only_when_stack_is_completed() {
        let world = World::new();
        let mut sink = Recorded::default();
        // stack が無い
        run(world
            .deploy()
            .ensure_post_deploy_state(&world.ctx, "web", &mut sink))
        .expect("無し");
        assert!(world.aws.calls().is_empty());
        // template が違う (REQUIRE_UPDATE)
        world.stacks.put(
            &world.stack_name(),
            FakeStacks::description(
                "UPDATE_COMPLETE",
                "0000000000000000",
                &[("DistributionId", DISTRIBUTION), ("TokenKvsArn", KVS_ARN)],
            ),
        );
        run(world
            .deploy()
            .ensure_post_deploy_state(&world.ctx, "web", &mut sink))
        .expect("古い");
        assert!(world.aws.calls().is_empty());
        // 完了済み: bucket policy と KVS だけを書く (stack と旧 bucket には触らない)
        run(world.deploy().update(&world.ctx, "web", &mut sink)).expect("update");
        world.aws.calls.borrow_mut().clear();
        world.aws.policies.borrow_mut().clear();
        let writes = world.stacks.writes().len();
        run(world
            .deploy()
            .ensure_post_deploy_state(&world.ctx, "web", &mut sink))
        .expect("完了");
        assert_eq!(world.stacks.writes().len(), writes);
        let calls = world.aws.calls();
        assert!(calls[0].starts_with("put_policy"));
        assert_eq!(calls.iter().filter(|c| c.starts_with("kvs_put")).count(), 2);
        assert!(!calls.iter().any(|c| c.starts_with("delete_bucket")));
    }

    #[test]
    fn test_kvs_is_skipped_without_values_or_outputs() {
        // 値が無い (store に secret が無い) なら KVS の API を呼ばない
        let mut world = World::new();
        world.store = FakeStore::default();
        let mut sink = Recorded::default();
        run(world.deploy().update(&world.ctx, "web", &mut sink)).expect("update");
        assert!(!world.aws.calls().iter().any(|c| c.starts_with("kvs_")));

        // TokenKvsArn の output が無ければ警告して書かない
        let world = World::new();
        world.stacks.outputs.borrow_mut().insert(
            world.stack_name(),
            vec![("DistributionId".to_string(), DISTRIBUTION.to_string())],
        );
        let mut sink = Recorded::default();
        run(world.deploy().update(&world.ctx, "web", &mut sink)).expect("update");
        assert!(!world.aws.calls().iter().any(|c| c.starts_with("kvs_")));
        assert!(sink
            .0
            .iter()
            .any(|r| r.1 == Style::Warning && r.2 == "TokenKvsArn が出力に見つかりません。"));
    }

    #[test]
    fn test_kvs_writes_only_configured_values_and_stops_on_failure() {
        // token_secret だけ値がある
        let mut world = World::new();
        let view = template_secrets::managed_secret_view(&world.ctx, "TOKEN_SECRET").expect("view");
        world.store = FakeStore::default().with(&view.pocket_key, json!({"TOKEN_SECRET": "tok"}));
        let mut sink = Recorded::default();
        run(world.deploy().update(&world.ctx, "web", &mut sink)).expect("update");
        let puts: Vec<String> = world
            .aws
            .calls()
            .into_iter()
            .filter(|c| c.starts_with("kvs_put"))
            .collect();
        assert_eq!(puts.len(), 1);
        assert!(puts[0].contains("token_secret=tok"));

        // API の失敗は中止
        let mut world = World::new();
        world.aws.kvs_fails = true;
        let mut sink = Recorded::default();
        let error = run(world.deploy().update(&world.ctx, "web", &mut sink)).expect_err("失敗");
        assert_eq!(error.to_string(), "PreconditionFailed");
    }

    #[test]
    fn test_delete_removes_policy_before_stack() {
        let world = World::new();
        let mut sink = Recorded::default();
        run(world.deploy().update(&world.ctx, "web", &mut sink)).expect("update");
        let cf = world.cf();
        // 他の statement は残す
        let other = json!({"Sid": "Other", "Effect": "Allow"});
        world.aws.policies.borrow_mut().insert(
            cf.bucket_name.clone(),
            json!({"Version": POLICY_VERSION, "Statement": [other, world.own()]}),
        );
        world.aws.calls.borrow_mut().clear();
        world.stacks.calls.borrow_mut().clear();
        let mut sink = Recorded::default();
        run(world.deploy().delete(cf, &mut sink)).expect("delete");
        assert_eq!(
            world.aws.calls(),
            [
                format!("delete_bucket {} example.com", cf.s3_region),
                format!("delete_bucket {} old.example.com", cf.s3_region),
                format!("put_policy {} {}", cf.s3_region, cf.bucket_name),
            ]
        );
        assert_eq!(
            world.aws.policies.borrow()[&cf.bucket_name],
            json!({"Version": POLICY_VERSION, "Statement": [other]})
        );
        assert_eq!(
            world.stacks.writes(),
            [format!("delete {} {}", cf.region, world.stack_name())]
        );
        assert!(sink
            .0
            .last()
            .is_some_and(|r| r.2.starts_with("S3 bucket is managed by the S3 resource: ")));
    }

    #[test]
    fn test_delete_requires_distribution_output() {
        let world = World::new();
        let mut sink = Recorded::default();
        let error = run(world.deploy().delete(world.cf(), &mut sink)).expect_err("stack 無し");
        assert_eq!(
            error.to_string(),
            "Cloudfront distribution is not created yet."
        );
        assert!(world.stacks.writes().is_empty());
    }

    #[test]
    fn test_default_waits_match_python() {
        let waits = CloudFrontWaits::default();
        assert_eq!(
            (waits.stack.attempts, waits.stack.interval.as_secs()),
            (180, 10)
        );
        assert_eq!(
            (waits.delete.attempts, waits.delete.interval.as_secs()),
            (60, 15)
        );
    }

    #[test]
    fn test_warn_contents() {
        let ctx = vector_context(VECTOR);
        let cf = find_cloudfront(&ctx, "web").expect("web");
        let mut sink = Recorded::default();
        warn_contents(cf, &mut sink);
        let lines: Vec<&str> = sink.0.iter().map(|r| r.2.as_str()).collect();
        // upload_dir のある route (default / assets / deploy) は案内しない
        assert!(lines.contains(
            &"/static/* は object_hash route です。`pocket static publish --stage dev` \
              で上げてください (deploy は触りません)"
        ));
        let media = format!(
            "Upload files manually to s3://{}/uploads/media/*",
            cf.bucket_name
        );
        assert!(lines.contains(&media.as_str()));
        assert!(!lines.iter().any(|line| line.contains("/assets/")));
    }
}
