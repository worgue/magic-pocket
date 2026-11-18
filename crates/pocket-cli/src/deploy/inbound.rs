//! SES 受信口 (inbound) の手順 (Python 版 `resources/inbound.py`)。
//!
//! 受信口 1 つ = stack 1 つ。receipt rule set は account / region で 1 つしか active に
//! できない共有物なので、deploy の前に差し込み先を解決し、deploy の後に実際の rule が
//! 宣言と一致するかを確かめる。削除は「受信を止めて deploy → 36 時間の配送猶予 → 滞留なし」
//! を確かめてからにする (配送中のメールと未処理の message を失わない)

use std::time::{Duration, SystemTime};

use anyhow::{bail, Context as _, Result};
use aws_config::SdkConfig;
use magic_pocket_settings::context::{Context, InboundContext};
use serde_json::{json, Value as Json};

use super::stacks::{wait, Stacks};
use crate::aws_util::{regional_config, sdk_error};
use crate::stack::inbound::inbound_stack;
use crate::stack::ops::{
    cfn_status, stack_outputs, status_detail, template_synced, CfnClient, ResourceStatus,
    WaitOptions,
};
use crate::stack::{StackOutputs, StackTemplate};
use crate::status::inbound::{resolve_rules, ActiveRuleSet, ReceiptRule};

/// 受信を止めてから削除までに置く配送猶予 (送信側の再送が尽きるまで)
pub const DELIVERY_GRACE: Duration = Duration::from_secs(36 * 60 * 60);

/// 削除の前に滞留を見る queue (stack の output の key)
const QUEUE_OUTPUTS: [&str; 3] = ["QueueUrl", "WorkerDLQUrl", "DeliveryDLQUrl"];

/// 変えると受信済みのデータや配送先と食い違う設定 (stack の output の key と今の値)
fn pinned_outputs(inlet: &InboundContext) -> [(&'static str, &str); 4] {
    let config = &inlet.config;
    [
        ("Handler", config.handler.as_str()),
        ("RawPrefix", config.raw_prefix.as_str()),
        ("MetadataPrefix", config.metadata_prefix.as_str()),
        ("ImportPrefix", config.import_prefix.as_str()),
    ]
}

pub trait InboundAws {
    async fn active_rule_set(&self, region: &str) -> Result<ActiveRuleSet>;
    /// `describe_receipt_rule` の `Rule` (boto3 の応答の形)
    async fn receipt_rule(&self, region: &str, rule_set: &str, name: &str) -> Result<Json>;
    /// deploy 済みの template のテキスト。stack が無ければ `None`
    async fn deployed_template(&self, region: &str, stack: &str) -> Result<Option<String>>;
    /// stack の最後の更新 (更新が無ければ作成) の時刻
    async fn stack_changed_at(&self, region: &str, stack: &str) -> Result<Option<SystemTime>>;
    /// queue にある message の数 (見えているもの・処理中・遅延中の合計)
    async fn queue_messages(&self, region: &str, queue_url: &str) -> Result<u64>;
}

/// stack の create / update / delete の完了待ち
#[derive(Debug, Clone, Copy)]
pub struct InboundWaits {
    pub stack: WaitOptions,
}

impl Default for InboundWaits {
    fn default() -> Self {
        Self {
            stack: WaitOptions::new(300, 10),
        }
    }
}

pub struct InboundDeploy<'a, K, A> {
    pub stacks: &'a K,
    pub aws: &'a A,
    pub waits: InboundWaits,
    /// 今の時刻 (配送猶予の判定)
    pub now: fn() -> SystemTime,
}

fn output<'o>(outputs: &'o StackOutputs, stack: &str, key: &str) -> Result<&'o str> {
    outputs
        .get(key)
        .map(String::as_str)
        .with_context(|| format!("inbound stack {stack} の output に {key} がありません"))
}

/// 宣言どおりなら SES にあるはずの rule (`describe_receipt_rule` の `Rule` の形)
pub fn expected_rule(inlet: &InboundContext, outputs: &StackOutputs) -> Result<Json> {
    let config = &inlet.config;
    let name = &inlet.resource_name;
    Ok(json!({
        "Name": name,
        "Enabled": config.enabled,
        "TlsPolicy": config.tls_policy,
        "ScanEnabled": config.scan_enabled,
        "Recipients": config.recipients,
        "Actions": [
            {
                "S3Action": {
                    "BucketName": output(outputs, name, "Bucket")?,
                    "ObjectKeyPrefix": config.raw_prefix,
                    "TopicArn": output(outputs, name, "TopicArn")?,
                }
            }
        ],
    }))
}

/// deploy 済みの template と比べて、今の宣言で更新してよいかを確かめる。
///
/// - 受信が有効なまま宛先を変えない (止めてから変える)
/// - 受信 bucket を置き換えない (bucket は Retain で、置き換えると受信済みが孤立する)
pub fn check_against_deployed(
    inlet: &InboundContext,
    deployed: &Json,
    current: &StackTemplate,
) -> Result<()> {
    let old_rule = &deployed["Resources"]["ReceiptRule"]["Properties"]["Rule"];
    let recipients = json!(inlet.config.recipients);
    if old_rule["Enabled"].as_bool() == Some(true) && old_rule["Recipients"] != recipients {
        bail!("宛先変更前に enabled = false でdeployし、滞留を処理してください");
    }
    let bucket_name =
        |template: &Json| template["Resources"]["Bucket"]["Properties"]["BucketName"].clone();
    let current: Json = serde_json::from_str(&current.body)
        .context("inbound の template が JSON ではありません")?;
    if bucket_name(deployed) != bucket_name(&current) {
        bail!("受信バケットの置換はできません。新しい受信口を作成してください");
    }
    Ok(())
}

impl<K: Stacks, A: InboundAws> InboundDeploy<'_, K, A> {
    /// 差し込み先を解決して template を描く。戻り値は (template, stack の output)
    async fn resolve(
        &self,
        inlet: &InboundContext,
    ) -> Result<(StackTemplate, Option<StackOutputs>)> {
        let description = self
            .stacks
            .describe(&inlet.region, &inlet.resource_name)
            .await?;
        let outputs = stack_outputs(description.as_ref());
        let active = self.aws.active_rule_set(&inlet.region).await?;
        let (rule_set, after) = resolve_rules(&active, inlet, outputs.as_ref())?;
        Ok((inbound_stack(inlet, &rule_set, after.as_deref()), outputs))
    }

    /// deploy の前の検査。stack を作る・更新する前に、今の宣言で進めてよいかを確かめる
    pub async fn prepare_deploy(&self, inlet: &InboundContext) -> Result<()> {
        let (current, outputs) = self.resolve(inlet).await?;
        let Some(outputs) = outputs else {
            return Ok(());
        };
        for (key, value) in pinned_outputs(inlet) {
            if outputs.get(key).map(String::as_str) != Some(value) {
                bail!("{key}の変更は新しいinbound名で移行してください");
            }
        }
        let deployed = self
            .aws
            .deployed_template(&inlet.region, &inlet.resource_name)
            .await?;
        let deployed: Json = match deployed.as_deref() {
            None | Some("") => return Ok(()),
            // template は JSON で置いている (JSON は YAML としても読める)
            Some(text) => serde_yaml_ng::from_str(text)
                .context("deploy 済みの inbound の template を読めません")?,
        };
        if deployed.is_null() {
            return Ok(());
        }
        check_against_deployed(inlet, &deployed, &current)
    }

    pub async fn create(&self, inlet: &InboundContext) -> Result<()> {
        let (stack, _) = self.resolve(inlet).await?;
        self.stacks.create(&stack).await?;
        self.wait(inlet, ResourceStatus::Completed).await
    }

    /// template が変わっていれば stack を更新する
    pub async fn update(&self, inlet: &InboundContext) -> Result<()> {
        let (stack, _) = self.resolve(inlet).await?;
        let description = self
            .stacks
            .describe(&inlet.region, &inlet.resource_name)
            .await?;
        if template_synced(description.as_ref(), &stack)? {
            return Ok(());
        }
        self.stacks.update(&stack).await?;
        self.wait(inlet, ResourceStatus::Completed).await
    }

    async fn wait(&self, inlet: &InboundContext, target: ResourceStatus) -> Result<()> {
        let (region, name) = (&inlet.region, &inlet.resource_name);
        wait(
            self.stacks,
            region,
            name,
            "inbound",
            target,
            self.waits.stack,
        )
        .await
    }

    /// deploy の後の確認。SES にある rule が宣言と完全に一致しなければ止める
    /// (stack の外で rule を書き換えられていると、受信の経路が宣言と食い違う)
    pub async fn ensure_post_deploy_state(&self, inlet: &InboundContext) -> Result<()> {
        let (_, outputs) = self.resolve(inlet).await?;
        let Some(outputs) = outputs else {
            bail!("受信スタックの出力を確認できません");
        };
        let name = &inlet.resource_name;
        let rule = self
            .aws
            .receipt_rule(
                &inlet.region,
                output(&outputs, name, "RuleSet")?,
                output(&outputs, name, "RuleName")?,
            )
            .await?;
        if rule != expected_rule(inlet, &outputs)? {
            bail!("SES受信ルールがdeployした宣言と一致しません");
        }
        Ok(())
    }

    /// 受信口を消す。受信を止めて deploy 済み・配送猶予が過ぎた・滞留が無い、の全部を
    /// 確かめてから stack を消す
    pub async fn delete(&self, inlet: &InboundContext) -> Result<()> {
        let (region, name) = (&inlet.region, &inlet.resource_name);
        let description = self.stacks.describe(region, name).await?;
        if status_detail(description.as_ref()) == "DELETE_IN_PROGRESS" {
            return self.wait(inlet, ResourceStatus::NoExist).await;
        }
        if cfn_status(description.as_ref())? == ResourceStatus::NoExist {
            return Ok(());
        }
        let Some(outputs) = stack_outputs(description.as_ref()) else {
            bail!("受信スタックの出力がありません。AWS上の状態を確認してください");
        };
        // 稼働中に削除を始めない。enabled = false で deploy した後に、猶予と滞留を見る
        let rule = self
            .aws
            .receipt_rule(
                region,
                output(&outputs, name, "RuleSet")?,
                output(&outputs, name, "RuleName")?,
            )
            .await?;
        let stopped = outputs.get("Enabled").map(String::as_str) == Some("false");
        if rule["Enabled"].as_bool() != Some(false) || !stopped {
            bail!(
                "先に inbound.enabled = false でdeployし、\
                 36時間以上の配送猶予を置いてください"
            );
        }
        let Some(changed) = self.aws.stack_changed_at(region, name).await? else {
            bail!("受信停止時刻を確認できません");
        };
        // 時刻が未来 (時計のずれ) なら経過 0 として扱う
        let elapsed = (self.now)().duration_since(changed).unwrap_or_default();
        if elapsed < DELIVERY_GRACE {
            bail!("最後のスタック更新から36時間の配送猶予が必要です");
        }
        for key in QUEUE_OUTPUTS {
            let url = output(&outputs, name, key)?;
            if self.aws.queue_messages(region, url).await? > 0 {
                bail!("{key} に滞留があります。処理または退避してから再実行してください");
            }
        }
        self.stacks.delete(region, name).await?;
        self.wait(inlet, ResourceStatus::NoExist).await
    }
}

/// state の記録にあって宣言から消えた inbound の stack が残っていれば止める。
///
/// 宣言だけ消す deploy を通すと、受信 rule と queue が管理の外に残る。
/// `saved` は state の `resources.inbound` (`{名前: {"stack_name": ...}}`)
pub async fn check_removed_inbound(
    ctx: &Context,
    saved: &Json,
    stacks: &impl Stacks,
) -> Result<()> {
    let Some(saved) = saved.as_object() else {
        return Ok(());
    };
    let region = match ctx.general.as_ref() {
        Some(general) => general.region.as_str(),
        None => bail!("general context is not configured"),
    };
    for (name, info) in saved {
        if ctx.inbound.contains_key(name) {
            continue;
        }
        let stack_name = info["stack_name"]
            .as_str()
            .with_context(|| format!("state の inbound.{name} に stack_name がありません"))?;
        let Some(description) = stacks.describe(region, stack_name).await? else {
            continue;
        };
        if description.status != "DELETE_COMPLETE" {
            bail!(
                "inbound.{name}がAWS上に残っています。宣言を戻して受信を停止し、\
                 inbound destroyを完了してから宣言を削除してください"
            );
        }
    }
    Ok(())
}

/// AWS に対する [`InboundAws`]
pub struct AwsInbound<'a> {
    pub sdk: &'a SdkConfig,
}

/// `describe_active_receipt_rule_set` の応答を、差し込み先の解決に使う形にする
pub async fn active_rule_set(sdk: &SdkConfig, region: &str) -> Result<ActiveRuleSet> {
    let client = aws_sdk_ses::Client::new(&regional_config(sdk, region));
    let response = client
        .describe_active_receipt_rule_set()
        .send()
        .await
        .map_err(|e| sdk_error("ses describe_active_receipt_rule_set", &e))?;
    Ok(ActiveRuleSet {
        name: response
            .metadata()
            .and_then(|metadata| metadata.name())
            .map(str::to_string),
        rules: response
            .rules()
            .iter()
            .map(|rule| ReceiptRule {
                name: rule.name().to_string(),
                enabled: rule.enabled(),
                recipients: rule.recipients().to_vec(),
                stopping: rule.actions().iter().any(|action| {
                    action.stop_action().is_some()
                        || action.bounce_action().is_some()
                        || action.lambda_action().is_some()
                }),
            })
            .collect(),
    })
}

/// SDK の rule を boto3 の応答の形 (設定されている key だけを持つ dict) にする。
///
/// S3Action 以外の action は種類だけを残す (宣言には無いので、在れば不一致になる)
fn rule_json(rule: &aws_sdk_ses::types::ReceiptRule) -> Json {
    let mut out = serde_json::Map::new();
    out.insert("Name".into(), json!(rule.name()));
    out.insert("Enabled".into(), json!(rule.enabled()));
    if let Some(policy) = rule.tls_policy() {
        out.insert("TlsPolicy".into(), json!(policy.as_str()));
    }
    out.insert("ScanEnabled".into(), json!(rule.scan_enabled()));
    if !rule.recipients().is_empty() {
        out.insert("Recipients".into(), json!(rule.recipients()));
    }
    let actions: Vec<Json> = rule
        .actions()
        .iter()
        .map(|action| {
            let mut kinds = serde_json::Map::new();
            if let Some(s3) = action.s3_action() {
                let mut body = serde_json::Map::new();
                let fields = [
                    ("TopicArn", s3.topic_arn()),
                    ("BucketName", Some(s3.bucket_name())),
                    ("ObjectKeyPrefix", s3.object_key_prefix()),
                    ("KmsKeyArn", s3.kms_key_arn()),
                    ("IamRoleArn", s3.iam_role_arn()),
                ];
                for (key, value) in fields {
                    if let Some(value) = value {
                        body.insert(key.into(), json!(value));
                    }
                }
                kinds.insert("S3Action".into(), Json::Object(body));
            }
            let others = [
                ("BounceAction", action.bounce_action().is_some()),
                ("WorkmailAction", action.workmail_action().is_some()),
                ("LambdaAction", action.lambda_action().is_some()),
                ("StopAction", action.stop_action().is_some()),
                ("AddHeaderAction", action.add_header_action().is_some()),
                ("SNSAction", action.sns_action().is_some()),
                ("ConnectAction", action.connect_action().is_some()),
            ];
            for (key, present) in others {
                if present {
                    kinds.insert(key.into(), json!({}));
                }
            }
            Json::Object(kinds)
        })
        .collect();
    if !actions.is_empty() {
        out.insert("Actions".into(), Json::Array(actions));
    }
    Json::Object(out)
}

impl InboundAws for AwsInbound<'_> {
    async fn active_rule_set(&self, region: &str) -> Result<ActiveRuleSet> {
        active_rule_set(self.sdk, region).await
    }

    async fn receipt_rule(&self, region: &str, rule_set: &str, name: &str) -> Result<Json> {
        let client = aws_sdk_ses::Client::new(&regional_config(self.sdk, region));
        let response = client
            .describe_receipt_rule()
            .rule_set_name(rule_set)
            .rule_name(name)
            .send()
            .await
            .map_err(|e| sdk_error("ses describe_receipt_rule", &e))?;
        let rule = response
            .rule()
            .context("describe_receipt_rule の応答に Rule がありません")?;
        Ok(rule_json(rule))
    }

    async fn deployed_template(&self, region: &str, stack: &str) -> Result<Option<String>> {
        CfnClient::new(self.sdk, region).template(stack).await
    }

    async fn stack_changed_at(&self, region: &str, stack: &str) -> Result<Option<SystemTime>> {
        let client = aws_sdk_cloudformation::Client::new(&regional_config(self.sdk, region));
        let response = client
            .describe_stacks()
            .stack_name(stack)
            .send()
            .await
            .map_err(|e| sdk_error("cloudformation describe_stacks", &e))?;
        let Some(found) = response.stacks().first() else {
            return Ok(None);
        };
        let changed = found.last_updated_time().or(found.creation_time());
        changed
            .map(|time| SystemTime::try_from(*time).context("stack の時刻を読めません"))
            .transpose()
    }

    async fn queue_messages(&self, region: &str, queue_url: &str) -> Result<u64> {
        use aws_sdk_sqs::types::QueueAttributeName as Name;

        let client = aws_sdk_sqs::Client::new(&regional_config(self.sdk, region));
        let response = client
            .get_queue_attributes()
            .queue_url(queue_url)
            .attribute_names(Name::ApproximateNumberOfMessages)
            .attribute_names(Name::ApproximateNumberOfMessagesNotVisible)
            .attribute_names(Name::ApproximateNumberOfMessagesDelayed)
            .send()
            .await
            .map_err(|e| sdk_error("sqs get_queue_attributes", &e))?;
        let mut total = 0;
        for (name, value) in response.attributes().into_iter().flatten() {
            let count: u64 = value
                .parse()
                .with_context(|| format!("{queue_url} の {name} を数として読めません: {value}"))?;
            total += count;
        }
        Ok(total)
    }
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;
    use std::collections::BTreeMap;

    use super::*;
    use crate::deploy::poll::testing::fast;
    use crate::deploy::stacks::fake::FakeStacks;
    use crate::stack::testing::vector_context;

    const RULE_SET: &str = "default-rule-set";
    const BUCKET: &str = "inbound-bucket-123456789012";
    const TOPIC: &str = "arn:aws:sns:ap-northeast-1:123456789012:inbound";

    fn context() -> Context {
        vector_context("example/example-dsql.sandbox")
    }

    fn inlet() -> InboundContext {
        context().inbound.values().next().unwrap().clone()
    }

    fn epoch(hours: u64) -> SystemTime {
        SystemTime::UNIX_EPOCH + Duration::from_secs(hours * 3600)
    }

    /// 今 = epoch から 100 時間後
    fn now() -> SystemTime {
        epoch(100)
    }

    #[derive(Default)]
    struct FakeAws {
        active: RefCell<ActiveRuleSet>,
        rule: RefCell<Json>,
        template: RefCell<Option<String>>,
        changed_at: RefCell<Option<SystemTime>>,
        queues: RefCell<BTreeMap<String, u64>>,
        calls: RefCell<Vec<String>>,
    }

    impl InboundAws for FakeAws {
        async fn active_rule_set(&self, _region: &str) -> Result<ActiveRuleSet> {
            Ok(self.active.borrow().clone())
        }

        async fn receipt_rule(&self, _region: &str, rule_set: &str, name: &str) -> Result<Json> {
            self.calls
                .borrow_mut()
                .push(format!("receipt_rule {rule_set} {name}"));
            Ok(self.rule.borrow().clone())
        }

        async fn deployed_template(&self, _region: &str, _stack: &str) -> Result<Option<String>> {
            Ok(self.template.borrow().clone())
        }

        async fn stack_changed_at(
            &self,
            _region: &str,
            _stack: &str,
        ) -> Result<Option<SystemTime>> {
            Ok(*self.changed_at.borrow())
        }

        async fn queue_messages(&self, _region: &str, queue_url: &str) -> Result<u64> {
            self.calls.borrow_mut().push(format!("queue {queue_url}"));
            Ok(self.queues.borrow().get(queue_url).copied().unwrap_or(0))
        }
    }

    fn own_rule(inlet: &InboundContext) -> ReceiptRule {
        ReceiptRule {
            name: inlet.resource_name.clone(),
            enabled: inlet.config.enabled,
            recipients: inlet.config.recipients.clone(),
            stopping: false,
        }
    }

    /// stack の output (deploy 済みの受信口)
    fn outputs(inlet: &InboundContext, enabled: bool) -> Vec<(String, String)> {
        let config = &inlet.config;
        [
            ("RuleSet", RULE_SET),
            ("RuleName", inlet.resource_name.as_str()),
            ("Bucket", BUCKET),
            ("TopicArn", TOPIC),
            ("Handler", config.handler.as_str()),
            ("RawPrefix", config.raw_prefix.as_str()),
            ("MetadataPrefix", config.metadata_prefix.as_str()),
            ("ImportPrefix", config.import_prefix.as_str()),
            ("Enabled", if enabled { "true" } else { "false" }),
            ("QueueUrl", "https://sqs.example/queue"),
            ("WorkerDLQUrl", "https://sqs.example/worker-dlq"),
            ("DeliveryDLQUrl", "https://sqs.example/delivery-dlq"),
        ]
        .iter()
        .map(|(key, value)| (key.to_string(), value.to_string()))
        .collect()
    }

    /// 今の宣言どおりに deploy 済みの状態 (stack・active な rule set・rule・template)
    fn deployed(inlet: &InboundContext, stacks: &FakeStacks, aws: &FakeAws) {
        *aws.active.borrow_mut() = ActiveRuleSet {
            name: Some(RULE_SET.to_string()),
            rules: vec![own_rule(inlet)],
        };
        let stack = inbound_stack(inlet, RULE_SET, None);
        let outputs = outputs(inlet, inlet.config.enabled);
        let pairs: Vec<(&str, &str)> = outputs
            .iter()
            .map(|(k, v)| (k.as_str(), v.as_str()))
            .collect();
        stacks.put_synced(&stack, &pairs);
        *aws.template.borrow_mut() = Some(stack.body.clone());
        let map: StackOutputs = outputs.into_iter().collect();
        *aws.rule.borrow_mut() = expected_rule(inlet, &map).unwrap();
    }

    fn deploy<'a>(
        stacks: &'a FakeStacks,
        aws: &'a FakeAws,
    ) -> InboundDeploy<'a, FakeStacks, FakeAws> {
        InboundDeploy {
            stacks,
            aws,
            waits: InboundWaits { stack: fast(3) },
            now,
        }
    }

    async fn error<T: std::fmt::Debug>(
        result: impl std::future::Future<Output = Result<T>>,
    ) -> String {
        result.await.unwrap_err().to_string()
    }

    #[tokio::test]
    async fn test_prepare_resolves_binding_before_first_deploy() {
        let (inlet, stacks, aws) = (inlet(), FakeStacks::default(), FakeAws::default());
        // active な rule set が無ければ、stack を作る前に止める
        assert!(error(deploy(&stacks, &aws).prepare_deploy(&inlet))
            .await
            .starts_with("active な receipt rule set がありません。"));
        // 宛先が重なる既存の rule
        *aws.active.borrow_mut() = ActiveRuleSet {
            name: Some(RULE_SET.to_string()),
            rules: vec![ReceiptRule {
                name: "other".to_string(),
                enabled: true,
                recipients: vec![],
                stopping: false,
            }],
        };
        assert_eq!(
            error(deploy(&stacks, &aws).prepare_deploy(&inlet)).await,
            "既存ルール other と競合します（宛先重複または停止・Lambda処理）"
        );
        aws.active.borrow_mut().rules.clear();
        deploy(&stacks, &aws).prepare_deploy(&inlet).await.unwrap();
        assert!(stacks.writes().is_empty());
    }

    #[tokio::test]
    async fn test_prepare_passes_for_unchanged_declaration() {
        let (inlet, stacks, aws) = (inlet(), FakeStacks::default(), FakeAws::default());
        deployed(&inlet, &stacks, &aws);
        deploy(&stacks, &aws).prepare_deploy(&inlet).await.unwrap();
        // template を読めない (空) 場合も通す
        *aws.template.borrow_mut() = None;
        deploy(&stacks, &aws).prepare_deploy(&inlet).await.unwrap();
    }

    #[tokio::test]
    async fn test_prepare_rejects_changes_to_pinned_settings() {
        let (base, stacks, aws) = (inlet(), FakeStacks::default(), FakeAws::default());
        deployed(&base, &stacks, &aws);
        type Change = fn(&mut InboundContext);
        let changes: [(&str, Change); 4] = [
            ("Handler", |i| i.config.handler = "web.other".to_string()),
            ("RawPrefix", |i| i.config.raw_prefix = "raw2/".to_string()),
            ("MetadataPrefix", |i| {
                i.config.metadata_prefix = "meta2/".to_string()
            }),
            ("ImportPrefix", |i| {
                i.config.import_prefix = "imports2/".to_string()
            }),
        ];
        for (key, change) in changes {
            let mut inlet = base.clone();
            change(&mut inlet);
            assert_eq!(
                error(deploy(&stacks, &aws).prepare_deploy(&inlet)).await,
                format!("{key}の変更は新しいinbound名で移行してください")
            );
        }
    }

    #[tokio::test]
    async fn test_prepare_rejects_recipient_change_while_enabled() {
        let (base, stacks, aws) = (inlet(), FakeStacks::default(), FakeAws::default());
        assert!(base.config.enabled);
        deployed(&base, &stacks, &aws);
        let mut inlet = base.clone();
        let domain = base.config.recipients[0].split('@').nth(1).unwrap();
        inlet.config.recipients = vec![format!("other@{domain}")];
        assert_eq!(
            error(deploy(&stacks, &aws).prepare_deploy(&inlet)).await,
            "宛先変更前に enabled = false でdeployし、滞留を処理してください"
        );
        // 受信を止めて deploy した後なら、宛先を変えられる
        let mut stopped = base.clone();
        stopped.config.enabled = false;
        *aws.template.borrow_mut() = Some(inbound_stack(&stopped, RULE_SET, None).body);
        deploy(&stacks, &aws).prepare_deploy(&inlet).await.unwrap();
    }

    #[tokio::test]
    async fn test_prepare_rejects_bucket_replacement() {
        let (inlet, stacks, aws) = (inlet(), FakeStacks::default(), FakeAws::default());
        deployed(&inlet, &stacks, &aws);
        let mut old: Json =
            serde_json::from_str(aws.template.borrow().as_deref().unwrap()).unwrap();
        old["Resources"]["Bucket"]["Properties"]["BucketName"] = json!("another-bucket");
        *aws.template.borrow_mut() = Some(old.to_string());
        assert_eq!(
            error(deploy(&stacks, &aws).prepare_deploy(&inlet)).await,
            "受信バケットの置換はできません。新しい受信口を作成してください"
        );
    }

    #[tokio::test]
    async fn test_create_and_update_follow_the_template() {
        let (inlet, stacks, aws) = (inlet(), FakeStacks::default(), FakeAws::default());
        *aws.active.borrow_mut() = ActiveRuleSet {
            name: Some(RULE_SET.to_string()),
            rules: vec![],
        };
        let region = inlet.region.clone();
        let name = inlet.resource_name.clone();
        deploy(&stacks, &aws).create(&inlet).await.unwrap();
        assert_eq!(stacks.writes(), [format!("create {region} {name}")]);
        // 作成の後は rule が rule set に在る。template が同じなら更新しない
        aws.active.borrow_mut().rules.push(own_rule(&inlet));
        stacks
            .outputs
            .borrow_mut()
            .insert(name.clone(), outputs(&inlet, true));
        deployed(&inlet, &stacks, &aws);
        deploy(&stacks, &aws).update(&inlet).await.unwrap();
        assert_eq!(stacks.writes().len(), 1);
        let mut changed = inlet.clone();
        changed.config.retention_days += 1;
        deploy(&stacks, &aws).update(&changed).await.unwrap();
        assert_eq!(stacks.writes()[1], format!("update {region} {name}"));
    }

    #[tokio::test]
    async fn test_post_deploy_requires_exact_rule() {
        let (inlet, stacks, aws) = (inlet(), FakeStacks::default(), FakeAws::default());
        // stack が無い
        *aws.active.borrow_mut() = ActiveRuleSet {
            name: Some(RULE_SET.to_string()),
            rules: vec![],
        };
        assert_eq!(
            error(deploy(&stacks, &aws).ensure_post_deploy_state(&inlet)).await,
            "受信スタックの出力を確認できません"
        );
        deployed(&inlet, &stacks, &aws);
        deploy(&stacks, &aws)
            .ensure_post_deploy_state(&inlet)
            .await
            .unwrap();
        assert_eq!(
            aws.calls.borrow().as_slice(),
            [format!("receipt_rule {RULE_SET} {}", inlet.resource_name)]
        );
        // stack の外で書き換えられた rule: 値の違い・余分な key・余分な action
        let good = aws.rule.borrow().clone();
        let mut disabled = good.clone();
        disabled["Enabled"] = json!(!inlet.config.enabled);
        let mut extra_key = good.clone();
        extra_key["Actions"][0]["S3Action"]["KmsKeyArn"] = json!("arn:aws:kms:key");
        let mut extra_action = good.clone();
        extra_action["Actions"]
            .as_array_mut()
            .unwrap()
            .push(json!({"StopAction": {}}));
        let mut recipients = good.clone();
        recipients["Recipients"] = json!(["someone@else.example.net"]);
        for rule in [disabled, extra_key, extra_action, recipients] {
            *aws.rule.borrow_mut() = rule;
            assert_eq!(
                error(deploy(&stacks, &aws).ensure_post_deploy_state(&inlet)).await,
                "SES受信ルールがdeployした宣言と一致しません"
            );
        }
    }

    /// 受信を止めて deploy 済み・`hours` 時間前に更新した受信口
    fn stopped(stacks: &FakeStacks, aws: &FakeAws, hours_ago: u64) -> InboundContext {
        let mut inlet = inlet();
        inlet.config.enabled = false;
        deployed(&inlet, stacks, aws);
        *aws.changed_at.borrow_mut() = Some(epoch(100 - hours_ago));
        inlet
    }

    #[tokio::test]
    async fn test_delete_after_grace_with_empty_queues() {
        let (stacks, aws) = (FakeStacks::default(), FakeAws::default());
        let inlet = stopped(&stacks, &aws, 36);
        deploy(&stacks, &aws).delete(&inlet).await.unwrap();
        assert_eq!(
            stacks.writes(),
            [format!("delete {} {}", inlet.region, inlet.resource_name)]
        );
        // rule → queue 3 つの順に全部見てから消す
        assert_eq!(
            aws.calls.borrow().as_slice(),
            [
                format!("receipt_rule {RULE_SET} {}", inlet.resource_name),
                "queue https://sqs.example/queue".to_string(),
                "queue https://sqs.example/worker-dlq".to_string(),
                "queue https://sqs.example/delivery-dlq".to_string(),
            ]
        );
    }

    #[tokio::test]
    async fn test_delete_requires_stopped_rule_and_grace() {
        let message =
            "先に inbound.enabled = false でdeployし、36時間以上の配送猶予を置いてください";
        // 受信が有効なまま
        let (stacks, aws) = (FakeStacks::default(), FakeAws::default());
        let inlet = inlet();
        deployed(&inlet, &stacks, &aws);
        *aws.changed_at.borrow_mut() = Some(epoch(0));
        assert_eq!(error(deploy(&stacks, &aws).delete(&inlet)).await, message);
        // output は止まっているが、実際の rule が有効 (stack の外で有効にされた)
        let (stacks, aws) = (FakeStacks::default(), FakeAws::default());
        let inlet = stopped(&stacks, &aws, 100);
        aws.rule.borrow_mut()["Enabled"] = json!(true);
        assert_eq!(error(deploy(&stacks, &aws).delete(&inlet)).await, message);
        // 猶予の境界: 36 時間ちょうどは通り、1 秒足りないと止まる
        let (stacks, aws) = (FakeStacks::default(), FakeAws::default());
        let inlet = stopped(&stacks, &aws, 36);
        *aws.changed_at.borrow_mut() = Some(epoch(64) + Duration::from_secs(1));
        assert_eq!(
            error(deploy(&stacks, &aws).delete(&inlet)).await,
            "最後のスタック更新から36時間の配送猶予が必要です"
        );
        *aws.changed_at.borrow_mut() = None;
        assert_eq!(
            error(deploy(&stacks, &aws).delete(&inlet)).await,
            "受信停止時刻を確認できません"
        );
        assert!(stacks.writes().is_empty());
    }

    #[tokio::test]
    async fn test_delete_stops_on_backlog() {
        for (key, url) in [
            ("QueueUrl", "https://sqs.example/queue"),
            ("WorkerDLQUrl", "https://sqs.example/worker-dlq"),
            ("DeliveryDLQUrl", "https://sqs.example/delivery-dlq"),
        ] {
            let (stacks, aws) = (FakeStacks::default(), FakeAws::default());
            let inlet = stopped(&stacks, &aws, 48);
            aws.queues.borrow_mut().insert(url.to_string(), 1);
            assert_eq!(
                error(deploy(&stacks, &aws).delete(&inlet)).await,
                format!("{key} に滞留があります。処理または退避してから再実行してください")
            );
            assert!(stacks.writes().is_empty());
        }
    }

    #[tokio::test]
    async fn test_delete_without_stack_or_while_deleting() {
        // stack が無ければ何もしない (検査もしない)
        let (inlet, stacks, aws) = (inlet(), FakeStacks::default(), FakeAws::default());
        deploy(&stacks, &aws).delete(&inlet).await.unwrap();
        assert!(stacks.writes().is_empty());
        assert!(aws.calls.borrow().is_empty());
        // 削除中なら待つだけ (もう一度 delete を出さない)
        stacks.put(
            &inlet.resource_name,
            FakeStacks::description("DELETE_IN_PROGRESS", "hash", &[]),
        );
        let result = error(deploy(&stacks, &aws).delete(&inlet)).await;
        assert!(result.contains("タイムアウト"), "{result}");
        assert!(stacks.writes().is_empty());
        assert!(aws.calls.borrow().is_empty());
        // output が無い stack は、状態を確かめられないので止める
        stacks.put(
            &inlet.resource_name,
            FakeStacks::description("CREATE_COMPLETE", "hash", &[]),
        );
        assert_eq!(
            error(deploy(&stacks, &aws).delete(&inlet)).await,
            "受信スタックの出力がありません。AWS上の状態を確認してください"
        );
    }

    #[tokio::test]
    async fn test_check_removed_inbound() {
        let ctx = context();
        let declared = ctx.inbound.keys().next().unwrap().clone();
        let stacks = FakeStacks::default();
        let saved = json!({
            declared.clone(): {"stack_name": "declared-stack"},
            "gone": {"stack_name": "gone-stack"},
            "deleted": {"stack_name": "deleted-stack"},
        });
        // 宣言に残る受信口の stack は見ない。stack が無い・DELETE_COMPLETE は通す
        stacks.put(
            "declared-stack",
            FakeStacks::description("UPDATE_COMPLETE", "h", &[]),
        );
        stacks.put(
            "deleted-stack",
            FakeStacks::description("DELETE_COMPLETE", "h", &[]),
        );
        check_removed_inbound(&ctx, &saved, &stacks).await.unwrap();
        assert!(!stacks
            .calls()
            .iter()
            .any(|call| call.contains("declared-stack")));
        // 宣言から消えたのに stack が残っている (削除中も含む)
        for status in ["UPDATE_COMPLETE", "DELETE_IN_PROGRESS", "DELETE_FAILED"] {
            stacks.put("gone-stack", FakeStacks::description(status, "h", &[]));
            assert_eq!(
                error(check_removed_inbound(&ctx, &saved, &stacks)).await,
                "inbound.goneがAWS上に残っています。宣言を戻して受信を停止し、\
                 inbound destroyを完了してから宣言を削除してください"
            );
        }
        assert!(stacks.writes().is_empty());
        // 記録が無い (初回の deploy)
        check_removed_inbound(&ctx, &Json::Null, &stacks)
            .await
            .unwrap();
        check_removed_inbound(&ctx, &json!({}), &stacks)
            .await
            .unwrap();
    }
}
