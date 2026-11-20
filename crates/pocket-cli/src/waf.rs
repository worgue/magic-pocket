//! `pocket-rs waf ip …` / `pocket-rs waf ratelimit …` (Python 版 `cli/waf_cli.py`)。
//!
//! WebACL / IPSet は CloudFormation (WAF の stack) が us-east-1 に作り、template 上の
//! Addresses は常に空。実際の CIDR の一覧はこのコマンドが UpdateIPSet で直接書き換える
//! (1 件の追加のために stack を更新しないため)。CloudFormation から見ると Addresses は
//! 常に drift しているが、これは仕様。
//!
//! Python 版との違い:
//! - `--stage` を省略したら入力を求めずエラー (env `POCKET_DEPLOY_STAGE` は読む)
//! - 自分の IP の検出に失敗したときの警告の error の文言は HTTP client の違いで異なる
//!
//! LockToken の競合 (WAFOptimisticLockException) は Python 版と同じく再試行せずに止める

use std::time::Duration;

use anyhow::{bail, Context as _, Result};
use aws_config::SdkConfig;
use clap::{Args, Subcommand};
use magic_pocket_settings::context::{CloudFrontContext, CloudFrontWafRateLimitContext, Context};
use magic_pocket_settings::model::RateLimitAction;

use crate::aws_util::{load_sdk, regional_config, sdk_error};
use crate::context::load_context_read_only;
use crate::deploy::edge::{format_rate_limit, EDGE_REGION};
use crate::deploy::interaction::{Interaction, Prompt};
use crate::stack::cloudfront::waf_stack_name;
use crate::stack::ops::{stack_outputs, CfnClient, StackApi as _};
use crate::stack::StackOutputs;
use crate::status::sink::{Sink, Terminal};

/// deploy 済みの WebACL の rule のうち、rate rule の名前の接頭辞
const RATE_LIMIT_RULE_PREFIX: &str = "ratelimit-";

/// 自分の IP を引く先 (1 次: AWS checkip、予備: ipify)
const SELF_IP_URLS: [&str; 2] = ["https://checkip.amazonaws.com", "https://api.ipify.org"];

#[derive(Subcommand)]
pub enum WafCommand {
    /// IPSet (IP allowlist) の運用
    Ip {
        #[command(subcommand)]
        command: IpCommand,
    },
    /// rate_limits (rate-based rule) の運用
    Ratelimit {
        #[command(subcommand)]
        command: RatelimitCommand,
    },
}

#[derive(Args, Clone)]
pub struct WafTarget {
    /// stage 名
    #[arg(long, env = "POCKET_DEPLOY_STAGE")]
    stage: String,
    /// CloudFront name (pocket.toml の key)
    #[arg(long)]
    name: String,
}

#[derive(Subcommand)]
pub enum IpCommand {
    /// IPSet 内 CIDR 一覧を表示する
    List {
        #[command(flatten)]
        target: WafTarget,
    },
    /// 任意 CIDR を IPSet に追加。`self` で自分の IP を追加 (重複は dedup)
    Add {
        #[command(flatten)]
        target: WafTarget,
        cidr: String,
    },
    /// 指定 CIDR を IPSet から削除
    Remove {
        #[command(flatten)]
        target: WafTarget,
        cidr: String,
    },
    /// IPSet を空にする (= deny-all 状態に戻す)。確認プロンプトあり
    Clear {
        #[command(flatten)]
        target: WafTarget,
        /// 確認プロンプトをスキップ
        #[arg(long)]
        yes: bool,
    },
}

#[derive(Subcommand)]
pub enum RatelimitCommand {
    /// deploy 済みの rate rule ごとに、レート超過として追跡中の IP を表示する
    ///
    /// WAF が「超過中」として扱っている IP の一覧 (managed keys) で、action が block なら
    /// 429 で止めている IP、count なら止めずに数えているだけの IP。超過の検知と解除には
    /// 数分の遅れがある
    List {
        #[command(flatten)]
        target: WafTarget,
    },
}

/// deploy 済みの WebACL の rule (rate rule の判定に要る分だけ)
#[derive(Debug, Clone, PartialEq)]
pub struct DeployedRule {
    pub name: String,
    pub priority: i32,
    /// RateBasedStatement (無ければ `None`)
    pub rate: Option<RateStatement>,
    /// Action が Block か
    pub block: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub struct RateStatement {
    pub limit: i64,
    pub window_sec: i64,
    /// ScopeDownStatement の ByteMatchStatement (SearchString, PositionalConstraint)
    pub byte_match: Option<(String, String)>,
}

/// WAF (Scope=CLOUDFRONT) の API と、自分の IP の検出
pub trait WafApi {
    /// GetIPSet: (Addresses, LockToken)
    async fn get_ip_set(&self, name: &str, id: &str) -> Result<(Vec<String>, String)>;
    /// UpdateIPSet
    async fn update_ip_set(
        &self,
        name: &str,
        id: &str,
        addresses: &[String],
        lock: &str,
    ) -> Result<()>;
    /// GetWebACL の Rules
    async fn web_acl_rules(&self, name: &str, id: &str) -> Result<Vec<DeployedRule>>;
    /// GetRateBasedStatementManagedKeys の IPv4 → IPv6 の Addresses
    async fn managed_keys(&self, acl_name: &str, acl_id: &str, rule: &str) -> Result<Vec<String>>;
    /// `url` の本文 (自分の IP の検出)
    async fn fetch_text(&self, url: &str) -> Result<String>;
}

pub async fn run(command: WafCommand) -> Result<()> {
    let target = match &command {
        WafCommand::Ip { command } => match command {
            IpCommand::List { target }
            | IpCommand::Add { target, .. }
            | IpCommand::Remove { target, .. }
            | IpCommand::Clear { target, .. } => target.clone(),
        },
        WafCommand::Ratelimit {
            command: RatelimitCommand::List { target },
        } => target.clone(),
    };
    let (context, _) = load_context_read_only(&target.stage)?;
    let sdk = load_sdk().await;
    let api = SdkWaf::new(&sdk);
    let mut sink = Terminal::new();
    let sink: &mut dyn Sink = &mut sink;
    let (stage, name) = (target.stage.as_str(), target.name.as_str());
    match command {
        WafCommand::Ip { command } => {
            let cf = cf_context(&context, name)?;
            let outputs = waf_outputs(&sdk, cf).await?;
            let stack = waf_stack_name(cf);
            match command {
                IpCommand::List { .. } => {
                    ip_list(&api, outputs.as_ref(), &stack, stage, name, sink).await
                }
                IpCommand::Add { cidr, .. } => {
                    ip_add(&api, outputs.as_ref(), &stack, &cidr, sink).await
                }
                IpCommand::Remove { cidr, .. } => {
                    ip_remove(&api, outputs.as_ref(), &stack, &cidr, sink).await
                }
                IpCommand::Clear { yes, .. } => {
                    let prompt = Interaction { assume_yes: yes };
                    ip_clear(&api, outputs.as_ref(), &stack, &prompt, sink).await
                }
            }
        }
        WafCommand::Ratelimit { .. } => {
            let cf = waf_cf_context(&context, name)?;
            let outputs = waf_outputs(&sdk, cf).await?;
            let stack = waf_stack_name(cf);
            ratelimit_list(&api, outputs.as_ref(), &stack, name, sink).await
        }
    }
}

/// Python の `_get_waf_cf_context`
fn waf_cf_context<'c>(context: &'c Context, name: &str) -> Result<&'c CloudFrontContext> {
    if context.cloudfront.is_empty() {
        bail!("cloudfront is not configured for this stage");
    }
    let Some(cf) = context.cloudfront.get(name) else {
        bail!("cloudfront '{name}' is not configured");
    };
    if cf.waf.is_none() {
        bail!("cloudfront '{name}' has no [cloudfront.{name}.waf] block");
    }
    Ok(cf)
}

/// Python の `_get_cf_context` (IPSet がある cloudfront)
fn cf_context<'c>(context: &'c Context, name: &str) -> Result<&'c CloudFrontContext> {
    let cf = waf_cf_context(context, name)?;
    if !cf.waf.as_ref().is_some_and(|waf| waf.enable_ip_set) {
        bail!(
            "cloudfront '{name}' is configured with [cloudfront.{name}.waf] enable_ip_set = false, \
             so there is no IPSet to operate on. Set `enable_ip_set = true` (default) to use \
             `pocket waf ip ...`."
        );
    }
    Ok(cf)
}

async fn waf_outputs(sdk: &SdkConfig, cf: &CloudFrontContext) -> Result<Option<StackOutputs>> {
    let description = CfnClient::new(sdk, EDGE_REGION)
        .describe(&waf_stack_name(cf))
        .await?;
    Ok(stack_outputs(description.as_ref()))
}

fn require_outputs<'o>(outputs: Option<&'o StackOutputs>, stack: &str) -> Result<&'o StackOutputs> {
    match outputs {
        Some(outputs) if !outputs.is_empty() => Ok(outputs),
        _ => {
            bail!("WAF stack '{stack}' が見つかりません。先に `pocket deploy` を実行してください。")
        }
    }
}

/// Python の `_get_ip_set_meta`: stack の output から IPSet の (Name, Id)
fn ip_set_meta<'o>(outputs: Option<&'o StackOutputs>, stack: &str) -> Result<(&'o str, &'o str)> {
    let outputs = require_outputs(outputs, stack)?;
    let get = |key: &str| {
        outputs
            .get(key)
            .map(String::as_str)
            .filter(|v| !v.is_empty())
    };
    match (get("IPSetName"), get("IPSetId")) {
        (Some(name), Some(id)) => Ok((name, id)),
        _ => bail!("WAF stack output に IPSetName / IPSetId がありません: {stack}"),
    }
}

/// Python の `_get_web_acl_meta`: output の WebACLArn
/// (`arn:aws:wafv2:us-east-1:<acct>:global/webacl/<name>/<id>`) から WebACL の (Name, Id)
fn web_acl_meta(outputs: Option<&StackOutputs>, stack: &str) -> Result<(String, String)> {
    let outputs = require_outputs(outputs, stack)?;
    let Some(arn) = outputs.get("WebACLArn").filter(|v| !v.is_empty()) else {
        bail!("WAF stack output に WebACLArn がありません: {stack}");
    };
    let mut parts = arn.rsplitn(3, '/');
    match (parts.next(), parts.next()) {
        (Some(id), Some(name)) => Ok((name.to_string(), id.to_string())),
        _ => bail!("WebACLArn の形式が想定と異なります: {arn}"),
    }
}

/// Python の `_detect_self_ipv4`: `/32` を付けた CIDR
async fn detect_self_ipv4(api: &impl WafApi, sink: &mut dyn Sink) -> Result<String> {
    for url in SELF_IP_URLS {
        match api.fetch_text(url).await {
            Ok(text) => {
                let ip = text.trim();
                if !ip.is_empty() {
                    return Ok(format!("{ip}/32"));
                }
            }
            Err(e) => sink.warning(&format!("IP 検出失敗 ({url}): {e:#}")),
        }
    }
    bail!("自分の Global IP を取得できませんでした")
}

pub async fn ip_list(
    api: &impl WafApi,
    outputs: Option<&StackOutputs>,
    stack: &str,
    stage: &str,
    name: &str,
    sink: &mut dyn Sink,
) -> Result<()> {
    let (set_name, set_id) = ip_set_meta(outputs, stack)?;
    let (addresses, _) = api.get_ip_set(set_name, set_id).await?;
    if addresses.is_empty() {
        sink.warning(&format!(
            "IPSet は空です (deny-all 状態)。`pocket waf ip add self --name {name} --stage \
             {stage}` で自分の IP を追加してください。"
        ));
        return Ok(());
    }
    for cidr in &addresses {
        sink.print(cidr);
    }
    Ok(())
}

pub async fn ip_add(
    api: &impl WafApi,
    outputs: Option<&StackOutputs>,
    stack: &str,
    cidr: &str,
    sink: &mut dyn Sink,
) -> Result<()> {
    let (set_name, set_id) = ip_set_meta(outputs, stack)?;
    let cidr = if cidr == "self" {
        let detected = detect_self_ipv4(api, sink).await?;
        sink.info(&format!("検出した自分の IP: {detected}"));
        detected
    } else {
        cidr.to_string()
    };
    let (mut addresses, lock) = api.get_ip_set(set_name, set_id).await?;
    if addresses.contains(&cidr) {
        sink.info(&format!("{cidr} は既に登録済みです。"));
        return Ok(());
    }
    addresses.push(cidr.clone());
    api.update_ip_set(set_name, set_id, &addresses, &lock)
        .await?;
    sink.success(&format!(
        "追加しました: {cidr} (合計 {} entries)",
        addresses.len()
    ));
    Ok(())
}

pub async fn ip_remove(
    api: &impl WafApi,
    outputs: Option<&StackOutputs>,
    stack: &str,
    cidr: &str,
    sink: &mut dyn Sink,
) -> Result<()> {
    let (set_name, set_id) = ip_set_meta(outputs, stack)?;
    let (addresses, lock) = api.get_ip_set(set_name, set_id).await?;
    if !addresses.iter().any(|a| a == cidr) {
        sink.warning(&format!("{cidr} は IPSet に存在しません。"));
        return Ok(());
    }
    let remaining: Vec<String> = addresses.into_iter().filter(|a| a != cidr).collect();
    api.update_ip_set(set_name, set_id, &remaining, &lock)
        .await?;
    sink.success(&format!(
        "削除しました: {cidr} (残り {} entries)",
        remaining.len()
    ));
    Ok(())
}

pub async fn ip_clear(
    api: &impl WafApi,
    outputs: Option<&StackOutputs>,
    stack: &str,
    prompt: &impl Prompt,
    sink: &mut dyn Sink,
) -> Result<()> {
    let (set_name, set_id) = ip_set_meta(outputs, stack)?;
    let (addresses, lock) = api.get_ip_set(set_name, set_id).await?;
    if addresses.is_empty() {
        sink.info("IPSet は既に空です。");
        return Ok(());
    }
    let text = format!(
        "{} 件の CIDR を全削除して deny-all 状態にします。よろしいですか？",
        addresses.len()
    );
    if !prompt.confirm(&text, false)? {
        bail!("Aborted!");
    }
    api.update_ip_set(set_name, set_id, &[], &lock).await?;
    sink.success("IPSet を全削除しました (deny-all 状態)。");
    Ok(())
}

/// Python の `_deployed_rate_limit_rules`: deploy 済みの WebACL から rate rule を読む
/// (ローカルの宣言ではなく実態を見る。pocket.toml を変えて deploy していない間や並べ替えの
/// 直後に、別の rule の IP を誤った説明で表示しないため)
fn deployed_rate_limit_rules(
    mut rules: Vec<DeployedRule>,
) -> Vec<(String, CloudFrontWafRateLimitContext)> {
    rules.sort_by_key(|rule| rule.priority);
    rules
        .into_iter()
        .filter(|rule| rule.name.starts_with(RATE_LIMIT_RULE_PREFIX))
        .filter_map(|rule| {
            let statement = rule.rate?;
            let path = statement.byte_match.map(|(search, constraint)| {
                if constraint == "STARTS_WITH" {
                    format!("{search}*")
                } else {
                    search
                }
            });
            let action = if rule.block {
                RateLimitAction::Block
            } else {
                RateLimitAction::Count
            };
            Some((
                rule.name,
                CloudFrontWafRateLimitContext {
                    path,
                    limit: statement.limit,
                    window_sec: statement.window_sec,
                    action,
                    path_positional_constraint: String::new(),
                    path_search_string: String::new(),
                },
            ))
        })
        .collect()
}

pub async fn ratelimit_list(
    api: &impl WafApi,
    outputs: Option<&StackOutputs>,
    stack: &str,
    name: &str,
    sink: &mut dyn Sink,
) -> Result<()> {
    let (acl_name, acl_id) = web_acl_meta(outputs, stack)?;
    let rules = deployed_rate_limit_rules(api.web_acl_rules(&acl_name, &acl_id).await?);
    if rules.is_empty() {
        sink.warning(&format!(
            "WebACL '{acl_name}' に rate rule がありません。[cloudfront.{name}.waf] の \
             rate_limits を宣言して deploy してください。"
        ));
        return Ok(());
    }
    for (rule_name, rule) in &rules {
        let addresses = api.managed_keys(&acl_name, &acl_id, rule_name).await?;
        sink.info(&format_rate_limit(rule_name, rule));
        if addresses.is_empty() {
            sink.print("  (レート超過として追跡中の IP はありません)");
            continue;
        }
        for address in &addresses {
            sink.print(&format!("  {address}"));
        }
    }
    Ok(())
}

/// [`WafApi`] の実装 (aws-sdk-wafv2。Scope=CLOUDFRONT は us-east-1 でしか操作できない)
pub struct SdkWaf {
    client: aws_sdk_wafv2::Client,
    http: reqwest::Client,
}

impl SdkWaf {
    pub fn new(sdk: &SdkConfig) -> Self {
        Self {
            client: aws_sdk_wafv2::Client::new(&regional_config(sdk, EDGE_REGION)),
            http: reqwest::Client::new(),
        }
    }
}

fn convert_rule(rule: &aws_sdk_wafv2::types::Rule) -> DeployedRule {
    let rate = rule
        .statement()
        .and_then(|s| s.rate_based_statement())
        .map(|statement| RateStatement {
            limit: statement.limit(),
            // Python 版は EvaluationWindowSec が無ければ 300 (AWS の既定) とみなす。
            // SDK は無いと 0 にする
            window_sec: match statement.evaluation_window_sec() {
                0 => 300,
                sec => sec,
            },
            byte_match: statement
                .scope_down_statement()
                .and_then(|s| s.byte_match_statement())
                .map(|byte_match| {
                    (
                        String::from_utf8_lossy(byte_match.search_string().as_ref()).into_owned(),
                        byte_match.positional_constraint().as_str().to_string(),
                    )
                }),
        });
    DeployedRule {
        name: rule.name().to_string(),
        priority: rule.priority(),
        rate,
        block: rule.action().and_then(|a| a.block()).is_some(),
    }
}

impl WafApi for SdkWaf {
    async fn get_ip_set(&self, name: &str, id: &str) -> Result<(Vec<String>, String)> {
        let response = self
            .client
            .get_ip_set()
            .name(name)
            .scope(aws_sdk_wafv2::types::Scope::Cloudfront)
            .id(id)
            .send()
            .await
            .map_err(|e| sdk_error("wafv2 get_ip_set", &e))?;
        let addresses = response
            .ip_set()
            .map(|set| set.addresses().to_vec())
            .unwrap_or_default();
        let lock = response
            .lock_token()
            .context("GetIPSet の応答に LockToken がありません")?;
        Ok((addresses, lock.to_string()))
    }

    async fn update_ip_set(
        &self,
        name: &str,
        id: &str,
        addresses: &[String],
        lock: &str,
    ) -> Result<()> {
        self.client
            .update_ip_set()
            .name(name)
            .scope(aws_sdk_wafv2::types::Scope::Cloudfront)
            .id(id)
            .set_addresses(Some(addresses.to_vec()))
            .lock_token(lock)
            .send()
            .await
            .map_err(|e| sdk_error("wafv2 update_ip_set", &e))?;
        Ok(())
    }

    async fn web_acl_rules(&self, name: &str, id: &str) -> Result<Vec<DeployedRule>> {
        let response = self
            .client
            .get_web_acl()
            .name(name)
            .scope(aws_sdk_wafv2::types::Scope::Cloudfront)
            .id(id)
            .send()
            .await
            .map_err(|e| sdk_error("wafv2 get_web_acl", &e))?;
        let acl = response
            .web_acl()
            .context("GetWebACL の応答に WebACL がありません")?;
        Ok(acl.rules().iter().map(convert_rule).collect())
    }

    async fn managed_keys(&self, acl_name: &str, acl_id: &str, rule: &str) -> Result<Vec<String>> {
        let response = self
            .client
            .get_rate_based_statement_managed_keys()
            .scope(aws_sdk_wafv2::types::Scope::Cloudfront)
            .web_acl_name(acl_name)
            .web_acl_id(acl_id)
            .rule_name(rule)
            .send()
            .await
            .map_err(|e| sdk_error("wafv2 get_rate_based_statement_managed_keys", &e))?;
        let mut addresses: Vec<String> = Vec::new();
        for set in [response.managed_keys_ipv4(), response.managed_keys_ipv6()]
            .into_iter()
            .flatten()
        {
            addresses.extend(set.addresses().iter().cloned());
        }
        Ok(addresses)
    }

    async fn fetch_text(&self, url: &str) -> Result<String> {
        let response = self
            .http
            .get(url)
            .timeout(Duration::from_secs(5))
            .send()
            .await?
            .error_for_status()?;
        Ok(response.text().await?)
    }
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;

    use super::*;
    use crate::deploy::interaction::testing::FakePrompt;
    use crate::status::sink::{Recorded, Stream, Style};

    #[derive(Default)]
    struct FakeWaf {
        addresses: RefCell<Vec<String>>,
        updates: RefCell<Vec<(Vec<String>, String)>>,
        rules: Vec<DeployedRule>,
        keys: Vec<(String, Vec<String>)>,
        /// url → 本文 (無ければ失敗)
        pages: Vec<(String, String)>,
        calls: RefCell<Vec<String>>,
    }

    impl WafApi for FakeWaf {
        async fn get_ip_set(&self, name: &str, id: &str) -> Result<(Vec<String>, String)> {
            self.calls
                .borrow_mut()
                .push(format!("get_ip_set {name} {id}"));
            Ok((self.addresses.borrow().clone(), "lock-1".into()))
        }

        async fn update_ip_set(
            &self,
            name: &str,
            id: &str,
            addresses: &[String],
            lock: &str,
        ) -> Result<()> {
            self.calls
                .borrow_mut()
                .push(format!("update_ip_set {name} {id}"));
            self.updates
                .borrow_mut()
                .push((addresses.to_vec(), lock.to_string()));
            *self.addresses.borrow_mut() = addresses.to_vec();
            Ok(())
        }

        async fn web_acl_rules(&self, name: &str, id: &str) -> Result<Vec<DeployedRule>> {
            self.calls
                .borrow_mut()
                .push(format!("get_web_acl {name} {id}"));
            Ok(self.rules.clone())
        }

        async fn managed_keys(
            &self,
            acl_name: &str,
            acl_id: &str,
            rule: &str,
        ) -> Result<Vec<String>> {
            self.calls
                .borrow_mut()
                .push(format!("managed_keys {acl_name} {acl_id} {rule}"));
            Ok(self
                .keys
                .iter()
                .find(|(name, _)| name == rule)
                .map(|(_, keys)| keys.clone())
                .unwrap_or_default())
        }

        async fn fetch_text(&self, url: &str) -> Result<String> {
            match self.pages.iter().find(|(u, _)| u == url) {
                Some((_, body)) => Ok(body.clone()),
                None => bail!("connection refused"),
            }
        }
    }

    fn with_addresses(addresses: &[&str]) -> FakeWaf {
        FakeWaf {
            addresses: RefCell::new(addresses.iter().map(|a| a.to_string()).collect()),
            ..FakeWaf::default()
        }
    }

    fn outputs() -> StackOutputs {
        let mut outputs = StackOutputs::new();
        outputs.insert("IPSetName".into(), "sandbox-proj-web-ipset".into());
        outputs.insert("IPSetId".into(), "set-id".into());
        outputs.insert(
            "WebACLArn".into(),
            "arn:aws:wafv2:us-east-1:123456789012:global/webacl/sandbox-proj-web-acl/acl-id".into(),
        );
        outputs
    }

    const STACK: &str = "sandbox-proj-web-waf";

    fn lines(sink: &Recorded) -> Vec<(Stream, Style, &str)> {
        sink.0
            .iter()
            .map(|(stream, style, text)| (*stream, *style, text.as_str()))
            .collect()
    }

    #[tokio::test]
    async fn test_list_prints_addresses_to_stdout() {
        let api = with_addresses(&["192.0.2.1/32", "198.51.100.0/24"]);
        let mut sink = Recorded::default();
        ip_list(&api, Some(&outputs()), STACK, "sandbox", "web", &mut sink)
            .await
            .unwrap();
        assert_eq!(
            lines(&sink),
            vec![
                (Stream::Stdout, Style::Plain, "192.0.2.1/32"),
                (Stream::Stdout, Style::Plain, "198.51.100.0/24"),
            ]
        );
        assert_eq!(
            *api.calls.borrow(),
            vec!["get_ip_set sandbox-proj-web-ipset set-id"]
        );
    }

    #[tokio::test]
    async fn test_list_empty_ip_set_warns() {
        let api = with_addresses(&[]);
        let mut sink = Recorded::default();
        ip_list(&api, Some(&outputs()), STACK, "sandbox", "web", &mut sink)
            .await
            .unwrap();
        assert_eq!(
            lines(&sink),
            vec![(
                Stream::Stderr,
                Style::Warning,
                "IPSet は空です (deny-all 状態)。`pocket waf ip add self --name web --stage \
                 sandbox` で自分の IP を追加してください。"
            )]
        );
    }

    #[tokio::test]
    async fn test_missing_stack_or_outputs() {
        let api = with_addresses(&[]);
        let mut sink = Recorded::default();
        let error = ip_list(&api, None, STACK, "sandbox", "web", &mut sink)
            .await
            .unwrap_err();
        assert_eq!(
            error.to_string(),
            "WAF stack 'sandbox-proj-web-waf' が見つかりません。先に `pocket deploy` を実行してください。"
        );
        let mut partial = StackOutputs::new();
        partial.insert("IPSetName".into(), "x".into());
        let error = ip_add(&api, Some(&partial), STACK, "192.0.2.1/32", &mut sink)
            .await
            .unwrap_err();
        assert_eq!(
            error.to_string(),
            "WAF stack output に IPSetName / IPSetId がありません: sandbox-proj-web-waf"
        );
        let error = ratelimit_list(&api, Some(&partial), STACK, "web", &mut sink)
            .await
            .unwrap_err();
        assert_eq!(
            error.to_string(),
            "WAF stack output に WebACLArn がありません: sandbox-proj-web-waf"
        );
        assert!(api.calls.borrow().is_empty());
    }

    #[tokio::test]
    async fn test_add_appends_with_lock_token() {
        let api = with_addresses(&["192.0.2.1/32"]);
        let mut sink = Recorded::default();
        ip_add(&api, Some(&outputs()), STACK, "198.51.100.0/24", &mut sink)
            .await
            .unwrap();
        assert_eq!(
            *api.updates.borrow(),
            vec![(
                vec!["192.0.2.1/32".to_string(), "198.51.100.0/24".to_string()],
                "lock-1".to_string()
            )]
        );
        assert_eq!(
            lines(&sink),
            vec![(
                Stream::Stderr,
                Style::Success,
                "追加しました: 198.51.100.0/24 (合計 2 entries)"
            )]
        );
    }

    #[tokio::test]
    async fn test_add_dedups_existing_cidr() {
        let api = with_addresses(&["192.0.2.1/32"]);
        let mut sink = Recorded::default();
        ip_add(&api, Some(&outputs()), STACK, "192.0.2.1/32", &mut sink)
            .await
            .unwrap();
        assert!(api.updates.borrow().is_empty());
        assert_eq!(
            lines(&sink),
            vec![(
                Stream::Stderr,
                Style::Info,
                "192.0.2.1/32 は既に登録済みです。"
            )]
        );
    }

    #[tokio::test]
    async fn test_add_self_falls_back_to_second_url() {
        let api = FakeWaf {
            pages: vec![("https://api.ipify.org".into(), "203.0.113.5\n".into())],
            ..FakeWaf::default()
        };
        let mut sink = Recorded::default();
        ip_add(&api, Some(&outputs()), STACK, "self", &mut sink)
            .await
            .unwrap();
        assert_eq!(
            lines(&sink),
            vec![
                (
                    Stream::Stderr,
                    Style::Warning,
                    "IP 検出失敗 (https://checkip.amazonaws.com): connection refused"
                ),
                (
                    Stream::Stderr,
                    Style::Info,
                    "検出した自分の IP: 203.0.113.5/32"
                ),
                (
                    Stream::Stderr,
                    Style::Success,
                    "追加しました: 203.0.113.5/32 (合計 1 entries)"
                ),
            ]
        );
    }

    #[tokio::test]
    async fn test_add_self_fails_when_no_url_answers() {
        let api = FakeWaf {
            pages: vec![("https://checkip.amazonaws.com".into(), "  \n".into())],
            ..FakeWaf::default()
        };
        let mut sink = Recorded::default();
        let error = ip_add(&api, Some(&outputs()), STACK, "self", &mut sink)
            .await
            .unwrap_err();
        assert_eq!(error.to_string(), "自分の Global IP を取得できませんでした");
        // 空の本文は警告せずに次へ進む (Python 版と同じ)
        assert_eq!(sink.0.len(), 1);
        assert!(api.updates.borrow().is_empty());
    }

    #[tokio::test]
    async fn test_remove_missing_cidr_warns_without_update() {
        let api = with_addresses(&["192.0.2.1/32"]);
        let mut sink = Recorded::default();
        ip_remove(&api, Some(&outputs()), STACK, "198.51.100.0/24", &mut sink)
            .await
            .unwrap();
        assert!(api.updates.borrow().is_empty());
        assert_eq!(
            lines(&sink),
            vec![(
                Stream::Stderr,
                Style::Warning,
                "198.51.100.0/24 は IPSet に存在しません。"
            )]
        );
    }

    #[tokio::test]
    async fn test_remove_existing_cidr() {
        let api = with_addresses(&["192.0.2.1/32", "198.51.100.0/24"]);
        let mut sink = Recorded::default();
        ip_remove(&api, Some(&outputs()), STACK, "192.0.2.1/32", &mut sink)
            .await
            .unwrap();
        assert_eq!(
            *api.updates.borrow(),
            vec![(vec!["198.51.100.0/24".to_string()], "lock-1".to_string())]
        );
        assert_eq!(
            lines(&sink),
            vec![(
                Stream::Stderr,
                Style::Success,
                "削除しました: 192.0.2.1/32 (残り 1 entries)"
            )]
        );
    }

    #[tokio::test]
    async fn test_clear() {
        // 空なら確認せずに終わる
        let api = with_addresses(&[]);
        let mut sink = Recorded::default();
        let prompt = FakePrompt::answering(true);
        ip_clear(&api, Some(&outputs()), STACK, &prompt, &mut sink)
            .await
            .unwrap();
        assert!(prompt.asked.borrow().is_empty());
        assert_eq!(
            lines(&sink),
            vec![(Stream::Stderr, Style::Info, "IPSet は既に空です。")]
        );

        // 断ったら書かない
        let api = with_addresses(&["192.0.2.1/32", "198.51.100.0/24"]);
        let prompt = FakePrompt::answering(false);
        let error = ip_clear(&api, Some(&outputs()), STACK, &prompt, &mut sink)
            .await
            .unwrap_err();
        assert_eq!(error.to_string(), "Aborted!");
        assert_eq!(
            *prompt.asked.borrow(),
            vec!["2 件の CIDR を全削除して deny-all 状態にします。よろしいですか？"]
        );
        assert!(api.updates.borrow().is_empty());

        // --yes は聞かずに空にする
        let mut sink = Recorded::default();
        ip_clear(&api, Some(&outputs()), STACK, &FakePrompt::yes(), &mut sink)
            .await
            .unwrap();
        assert_eq!(*api.updates.borrow(), vec![(vec![], "lock-1".to_string())]);
        assert_eq!(
            lines(&sink),
            vec![(
                Stream::Stderr,
                Style::Success,
                "IPSet を全削除しました (deny-all 状態)。"
            )]
        );
    }

    fn rate_rule(
        name: &str,
        priority: i32,
        rate: Option<RateStatement>,
        block: bool,
    ) -> DeployedRule {
        DeployedRule {
            name: name.into(),
            priority,
            rate,
            block,
        }
    }

    #[tokio::test]
    async fn test_ratelimit_list_reads_deployed_rules_in_priority_order() {
        let api = FakeWaf {
            rules: vec![
                rate_rule(
                    "ratelimit-1",
                    20,
                    Some(RateStatement {
                        limit: 100,
                        window_sec: 60,
                        byte_match: Some(("/api/".into(), "STARTS_WITH".into())),
                    }),
                    false,
                ),
                // rate rule でない rule・名前の違う rate rule は対象外
                rate_rule("allow-ip", 0, None, false),
                rate_rule(
                    "other-rate",
                    5,
                    Some(RateStatement {
                        limit: 10,
                        window_sec: 300,
                        byte_match: None,
                    }),
                    true,
                ),
                rate_rule("ratelimit-x", 1, None, true),
                rate_rule(
                    "ratelimit-0",
                    10,
                    Some(RateStatement {
                        limit: 2000,
                        window_sec: 300,
                        byte_match: Some(("/login".into(), "EXACTLY".into())),
                    }),
                    true,
                ),
                rate_rule(
                    "ratelimit-2",
                    30,
                    Some(RateStatement {
                        limit: 500,
                        window_sec: 300,
                        byte_match: None,
                    }),
                    true,
                ),
            ],
            keys: vec![(
                "ratelimit-0".into(),
                vec!["192.0.2.1/32".into(), "2001:db8::1/128".into()],
            )],
            ..FakeWaf::default()
        };
        let mut sink = Recorded::default();
        ratelimit_list(&api, Some(&outputs()), STACK, "web", &mut sink)
            .await
            .unwrap();
        assert_eq!(
            lines(&sink),
            vec![
                (
                    Stream::Stderr,
                    Style::Info,
                    "ratelimit-0: 2000 req / 300 秒, path=/login, action=block"
                ),
                (Stream::Stdout, Style::Plain, "  192.0.2.1/32"),
                (Stream::Stdout, Style::Plain, "  2001:db8::1/128"),
                (
                    Stream::Stderr,
                    Style::Info,
                    "ratelimit-1: 100 req / 60 秒, path=/api/*, action=count"
                ),
                (
                    Stream::Stdout,
                    Style::Plain,
                    "  (レート超過として追跡中の IP はありません)"
                ),
                (
                    Stream::Stderr,
                    Style::Info,
                    "ratelimit-2: 500 req / 300 秒, path=(全リクエスト), action=block"
                ),
                (
                    Stream::Stdout,
                    Style::Plain,
                    "  (レート超過として追跡中の IP はありません)"
                ),
            ]
        );
        assert_eq!(
            api.calls.borrow()[0],
            "get_web_acl sandbox-proj-web-acl acl-id"
        );
    }

    #[tokio::test]
    async fn test_ratelimit_list_without_rate_rules_warns() {
        let api = FakeWaf {
            rules: vec![rate_rule("allow-ip", 0, None, false)],
            ..FakeWaf::default()
        };
        let mut sink = Recorded::default();
        ratelimit_list(&api, Some(&outputs()), STACK, "web", &mut sink)
            .await
            .unwrap();
        assert_eq!(
            lines(&sink),
            vec![(
                Stream::Stderr,
                Style::Warning,
                "WebACL 'sandbox-proj-web-acl' に rate rule がありません。[cloudfront.web.waf] の \
                 rate_limits を宣言して deploy してください。"
            )]
        );
        assert_eq!(api.calls.borrow().len(), 1);
    }

    #[test]
    fn test_convert_rule_reads_sdk_shapes() {
        use aws_sdk_wafv2::primitives::Blob;
        use aws_sdk_wafv2::types::{
            BlockAction, ByteMatchStatement, FieldToMatch, PositionalConstraint,
            RateBasedStatement, RateBasedStatementAggregateKeyType, Rule, RuleAction, Statement,
            TextTransformation, TextTransformationType, UriPath, VisibilityConfig,
        };
        let scope_down = Statement::builder()
            .byte_match_statement(
                ByteMatchStatement::builder()
                    .search_string(Blob::new("/api/"))
                    .field_to_match(
                        FieldToMatch::builder()
                            .uri_path(UriPath::builder().build())
                            .build(),
                    )
                    .text_transformations(
                        TextTransformation::builder()
                            .priority(0)
                            .r#type(TextTransformationType::None)
                            .build()
                            .unwrap(),
                    )
                    .positional_constraint(PositionalConstraint::StartsWith)
                    .build()
                    .unwrap(),
            )
            .build();
        let rule = Rule::builder()
            .name("ratelimit-0")
            .priority(3)
            .statement(
                Statement::builder()
                    .rate_based_statement(
                        RateBasedStatement::builder()
                            .limit(100)
                            .aggregate_key_type(RateBasedStatementAggregateKeyType::Ip)
                            .scope_down_statement(scope_down)
                            .build()
                            .unwrap(),
                    )
                    .build(),
            )
            .action(
                RuleAction::builder()
                    .block(BlockAction::builder().build())
                    .build(),
            )
            .visibility_config(
                VisibilityConfig::builder()
                    .sampled_requests_enabled(false)
                    .cloud_watch_metrics_enabled(false)
                    .metric_name("m")
                    .build()
                    .unwrap(),
            )
            .build()
            .unwrap();
        assert_eq!(
            convert_rule(&rule),
            DeployedRule {
                name: "ratelimit-0".into(),
                priority: 3,
                rate: Some(RateStatement {
                    limit: 100,
                    // EvaluationWindowSec が無ければ 300
                    window_sec: 300,
                    byte_match: Some(("/api/".into(), "STARTS_WITH".into())),
                }),
                block: true,
            }
        );
    }
}
