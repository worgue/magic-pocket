//! 受信ドメイン (SES identity / DKIM / MX) の stack の手順 (Python 版
//! `resources/inbound_domain.py`)。
//!
//! 受信ドメイン 1 つ = stack 1 つ。pocket が DNS を持つドメイン (`manage_dns`) は deploy の
//! 最後に検証の完了まで待ち、外部 DNS のドメインは待たずに登録すべき record を示す。
//! 既に在る SES identity は取り込まない

use std::time::Instant;

use anyhow::{bail, Context as _, Result};
use aws_config::SdkConfig;
use magic_pocket_settings::context::{Context, HostedZone, InboundDomainContext};
use serde_json::Value as Json;

use super::stacks::{wait, Stacks};
use crate::aws_util::{has_code, regional_config, sdk_error};
use crate::stack::inbound::inbound_domain_stack;
use crate::stack::ops::{cfn_status, stack_outputs, template_synced, ResourceStatus, WaitOptions};
use crate::stack::{StackOutputs, StackTemplate};
use crate::status::sink::Sink;

const STATUS_COMMAND: &str = "pocket resource inbound --stage <stage> --name <name> status";

/// DKIM の token の番号 (stack の output `DkimName<i>` / `DkimValue<i>`)
const DKIM_TOKENS: [u8; 3] = [1, 2, 3];

pub trait InboundDomainAws {
    /// SESv2 の `VerificationStatus`。identity が無ければ `None`
    async fn verification_status(&self, region: &str, domain: &str) -> Result<Option<String>>;
    /// domain を含む hosted zone の id (override があればそれ)。見つからなければ error
    /// (外部 DNS は `manage_dns = false` を明示させ、黙って手動の登録に切り替えない)
    async fn hosted_zone_id(&self, domain: &str, override_id: Option<&str>) -> Result<String>;
}

#[derive(Debug, Clone, Copy)]
pub struct InboundDomainWaits {
    /// stack の create / update / delete の完了待ち
    pub stack: WaitOptions,
    /// SES の検証の完了待ち (同じ account の zone なら数分で終わる)
    pub verify: WaitOptions,
}

impl Default for InboundDomainWaits {
    fn default() -> Self {
        Self {
            stack: WaitOptions::new(300, 10),
            verify: WaitOptions::new(600, 15),
        }
    }
}

pub struct InboundDomainDeploy<'a, K, A> {
    pub stacks: &'a K,
    pub aws: &'a A,
    pub waits: InboundDomainWaits,
}

/// 登録が要る DNS record (type, name, value)。DKIM の CNAME 3 つと MX
pub fn dns_records(
    domain: &InboundDomainContext,
    outputs: &StackOutputs,
) -> Result<Vec<(String, String, String)>> {
    let get = |key: String| {
        outputs.get(&key).cloned().with_context(|| {
            format!(
                "inbound domain stack {} の output に {key} がありません",
                domain.stack_name
            )
        })
    };
    let mut records = Vec::new();
    for i in DKIM_TOKENS {
        records.push((
            "CNAME".to_string(),
            get(format!("DkimName{i}"))?,
            get(format!("DkimValue{i}"))?,
        ));
    }
    records.push((
        "MX".to_string(),
        domain.domain.clone(),
        get("MxValue".to_string())?,
    ));
    Ok(records)
}

/// Python の `str(None)` / 文字列そのまま
fn status_text(status: Option<&str>) -> &str {
    status.unwrap_or("None")
}

impl<K: Stacks, A: InboundDomainAws> InboundDomainDeploy<'_, K, A> {
    async fn template(&self, domain: &InboundDomainContext) -> Result<StackTemplate> {
        let zone = if domain.manage_dns {
            let override_id = domain.hosted_zone_id_override.as_deref();
            Some(self.aws.hosted_zone_id(&domain.domain, override_id).await?)
        } else {
            None
        };
        Ok(inbound_domain_stack(domain, zone.as_deref()))
    }

    /// deploy の前の検査。stack が無いのに identity が在れば止める
    pub async fn prepare_deploy(&self, domain: &InboundDomainContext) -> Result<()> {
        let description = self
            .stacks
            .describe(&domain.region, &domain.stack_name)
            .await?;
        if cfn_status(description.as_ref())? != ResourceStatus::NoExist {
            return Ok(());
        }
        let status = self
            .aws
            .verification_status(&domain.region, &domain.domain)
            .await?;
        if status.is_none() {
            return Ok(());
        }
        bail!(
            "{domain} のSES identityが既に存在します。\
             pocketは既存のidentityを取り込みません。\
             受信を別のサブドメインにするか、次で削除してからdeployしてください\
             （手で登録した検証用・MXのDNSレコードも削除が必要です）:\n  \
             aws sesv2 delete-email-identity --email-identity {domain} --region {region}",
            domain = domain.domain,
            region = domain.region
        )
    }

    pub async fn create(&self, domain: &InboundDomainContext) -> Result<()> {
        let stack = self.template(domain).await?;
        self.stacks.create(&stack).await?;
        self.wait(domain, ResourceStatus::Completed).await
    }

    /// template が変わっていれば stack を更新する
    pub async fn update(&self, domain: &InboundDomainContext) -> Result<()> {
        let stack = self.template(domain).await?;
        let description = self
            .stacks
            .describe(&domain.region, &domain.stack_name)
            .await?;
        if template_synced(description.as_ref(), &stack)? {
            return Ok(());
        }
        self.stacks.update(&stack).await?;
        self.wait(domain, ResourceStatus::Completed).await
    }

    pub async fn delete(&self, domain: &InboundDomainContext) -> Result<()> {
        let (region, name) = (&domain.region, &domain.stack_name);
        let description = self.stacks.describe(region, name).await?;
        if cfn_status(description.as_ref())? == ResourceStatus::NoExist {
            return Ok(());
        }
        self.stacks.delete(region, name).await?;
        self.wait(domain, ResourceStatus::NoExist).await
    }

    async fn wait(&self, domain: &InboundDomainContext, target: ResourceStatus) -> Result<()> {
        let (region, name) = (&domain.region, &domain.stack_name);
        let options = self.waits.stack;
        wait(self.stacks, region, name, "inbound_domain", target, options).await
    }

    /// 検証が SUCCESS になるまで待ち、待った秒数を返す
    pub async fn wait_verified(
        &self,
        domain: &InboundDomainContext,
        sink: &mut dyn Sink,
    ) -> Result<u64> {
        let started = Instant::now();
        let options = self.waits.verify;
        for attempt in 0..=options.attempts {
            let status = self
                .aws
                .verification_status(&domain.region, &domain.domain)
                .await?;
            let status = status_text(status.as_deref());
            if status == "SUCCESS" {
                return Ok(started.elapsed().as_secs_f64().round() as u64);
            }
            if attempt == options.attempts {
                bail!(
                    "{} のSES検証が完了しません（status={status}）。zoneの委任を確認し、\
                     検証後に再度deployしてください。検証完了までは受信できません。\
                     状態は {STATUS_COMMAND} で確認できます",
                    domain.domain
                );
            }
            sink.log(&format!(
                "Waiting for SES verification of {}... (status={status})",
                domain.domain
            ));
            tokio::time::sleep(options.interval).await;
        }
        unreachable!("最後の回で return か bail する")
    }

    /// deploy の最後に、受信できる状態かを確定する。
    ///
    /// pocket が DNS を持つドメインは検証の完了まで待つ。外部 DNS は待たずに、
    /// 登録すべき record と今の検証状態を示す
    pub async fn confirm(&self, ctx: &Context, sink: &mut dyn Sink) -> Result<()> {
        for domain in ctx.inbound_domain.values() {
            let name = &domain.domain;
            if domain.manage_dns {
                let waited = self.wait_verified(domain, sink).await?;
                sink.info(&format!(
                    "inbound domain {name}: verification=SUCCESS (waited {waited}s)"
                ));
                continue;
            }
            let status = self.aws.verification_status(&domain.region, name).await?;
            let status = status_text(status.as_deref());
            if status == "SUCCESS" {
                sink.info(&format!(
                    "inbound domain {name}: verification=SUCCESS (manage_dns = false)"
                ));
                continue;
            }
            sink.warning(&format!(
                "inbound domain {name}: verification={status} \
                 (manage_dns = false のため待たない)。\
                 次のレコードをDNSに登録してください。検証完了までは受信できません。\
                 状態は {STATUS_COMMAND} で確認できます"
            ));
            let description = self
                .stacks
                .describe(&domain.region, &domain.stack_name)
                .await?;
            // stack が未作成なら示す record が無い
            if let Some(outputs) = stack_outputs(description.as_ref()) {
                for (record_type, name, value) in dns_records(domain, &outputs)? {
                    sink.print(&format!("  {record_type} {name} {value}"));
                }
            }
        }
        Ok(())
    }
}

/// どの inbound も使わなくなったドメインの stack を消す (inbound の domain を変えた後)。
///
/// 削除の完了は待たない。`saved` は state の `resources.inbound_domain`
/// (`{ドメイン: {"stack_name": ...}}`)
pub async fn cleanup_unused_inbound_domains(
    ctx: &Context,
    saved: &Json,
    stacks: &impl Stacks,
    sink: &mut dyn Sink,
) -> Result<()> {
    let Some(saved) = saved.as_object() else {
        return Ok(());
    };
    let region = match ctx.general.as_ref() {
        Some(general) => general.region.as_str(),
        None => bail!("general context is not configured"),
    };
    for (domain, info) in saved {
        if ctx.inbound_domain.contains_key(domain) {
            continue;
        }
        let stack_name = info["stack_name"].as_str().with_context(|| {
            format!("state の inbound_domain.{domain} に stack_name がありません")
        })?;
        if stacks.describe(region, stack_name).await?.is_none() {
            continue;
        }
        sink.log(&format!("Deleting unused inbound domain {domain}..."));
        stacks.delete(region, stack_name).await?;
    }
    Ok(())
}

/// AWS に対する [`InboundDomainAws`]
pub struct AwsInboundDomain<'a> {
    pub sdk: &'a SdkConfig,
}

impl InboundDomainAws for AwsInboundDomain<'_> {
    async fn verification_status(&self, region: &str, domain: &str) -> Result<Option<String>> {
        let client = aws_sdk_sesv2::Client::new(&regional_config(self.sdk, region));
        match client
            .get_email_identity()
            .email_identity(domain)
            .send()
            .await
        {
            Ok(response) => Ok(response
                .verification_status()
                .map(|status| status.as_str().to_string())),
            Err(e) if has_code(&e, &["NotFoundException"]) => Ok(None),
            Err(e) => Err(sdk_error("sesv2 get_email_identity", &e)),
        }
    }

    async fn hosted_zone_id(&self, domain: &str, override_id: Option<&str>) -> Result<String> {
        if let Some(id) = override_id.filter(|id| !id.is_empty()) {
            return Ok(id.to_string());
        }
        let client = aws_sdk_route53::Client::new(self.sdk);
        let response = client
            .list_hosted_zones()
            .send()
            .await
            .map_err(|e| sdk_error("route53 list_hosted_zones", &e))?;
        if response.is_truncated() {
            bail!("Route53 hosted zone list is truncated. Please set hosted_zone_id.");
        }
        let zones: Vec<HostedZone> = response
            .hosted_zones()
            .iter()
            .map(|zone| HostedZone {
                name: zone.name().to_string(),
                id: zone.id().to_string(),
            })
            .collect();
        let Some(zone) = crate::status::aws::find_hosted_zone(&zones, domain) else {
            bail!(
                "No route53 hosted zone for the domain. [{domain}]\n\
                 Check your route53 hosted zone or set hosted_zone_id in pocket.toml"
            );
        };
        Ok(zone
            .id
            .get("/hostedzone/".len()..)
            .unwrap_or_default()
            .to_string())
    }
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;

    use serde_json::json;

    use super::*;
    use crate::deploy::poll::testing::fast;
    use crate::deploy::stacks::fake::FakeStacks;
    use crate::stack::testing::vector_context;
    use crate::status::sink::{Recorded, Stream, Style};

    fn context() -> Context {
        vector_context("example/example-dsql.sandbox")
    }

    fn domain() -> InboundDomainContext {
        context().inbound_domain.values().next().unwrap().clone()
    }

    #[derive(Default)]
    struct FakeAws {
        /// `verification_status` が順に返す値 (尽きたら最後の値)
        statuses: RefCell<Vec<Option<&'static str>>>,
        calls: RefCell<Vec<String>>,
    }

    impl FakeAws {
        fn with(statuses: &[Option<&'static str>]) -> Self {
            Self {
                statuses: RefCell::new(statuses.to_vec()),
                calls: RefCell::default(),
            }
        }
    }

    impl InboundDomainAws for FakeAws {
        async fn verification_status(&self, _region: &str, domain: &str) -> Result<Option<String>> {
            self.calls.borrow_mut().push(format!("status {domain}"));
            let mut statuses = self.statuses.borrow_mut();
            let status = if statuses.len() > 1 {
                statuses.remove(0)
            } else {
                statuses.first().copied().flatten()
            };
            Ok(status.map(str::to_string))
        }

        async fn hosted_zone_id(&self, domain: &str, override_id: Option<&str>) -> Result<String> {
            self.calls.borrow_mut().push(format!("zone {domain}"));
            Ok(override_id.unwrap_or("Z0ZONE").to_string())
        }
    }

    fn deploy<'a>(
        stacks: &'a FakeStacks,
        aws: &'a FakeAws,
    ) -> InboundDomainDeploy<'a, FakeStacks, FakeAws> {
        InboundDomainDeploy {
            stacks,
            aws,
            waits: InboundDomainWaits {
                stack: fast(3),
                verify: fast(3),
            },
        }
    }

    fn dkim_outputs() -> Vec<(&'static str, &'static str)> {
        vec![
            ("DkimName1", "t1._domainkey.in.example.com"),
            ("DkimValue1", "t1.dkim.amazonses.com"),
            ("DkimName2", "t2._domainkey.in.example.com"),
            ("DkimValue2", "t2.dkim.amazonses.com"),
            ("DkimName3", "t3._domainkey.in.example.com"),
            ("DkimValue3", "t3.dkim.amazonses.com"),
            ("MxValue", "10 inbound-smtp.ap-northeast-1.amazonaws.com"),
        ]
    }

    #[tokio::test]
    async fn test_prepare_rejects_identity_that_pocket_did_not_create() {
        let (domain, stacks) = (domain(), FakeStacks::default());
        // stack も identity も無い: 通す
        let aws = FakeAws::with(&[None]);
        deploy(&stacks, &aws).prepare_deploy(&domain).await.unwrap();
        // stack が無いのに identity が在る (検証が済んでいなくても同じ)
        for status in ["SUCCESS", "PENDING"] {
            let aws = FakeAws::with(&[Some(status)]);
            let error = deploy(&stacks, &aws)
                .prepare_deploy(&domain)
                .await
                .unwrap_err()
                .to_string();
            assert_eq!(
                error,
                format!(
                    "{d} のSES identityが既に存在します。pocketは既存のidentityを取り込みません。\
                     受信を別のサブドメインにするか、次で削除してからdeployしてください\
                     （手で登録した検証用・MXのDNSレコードも削除が必要です）:\n  \
                     aws sesv2 delete-email-identity --email-identity {d} --region {r}",
                    d = domain.domain,
                    r = domain.region
                )
            );
        }
        // stack が在れば identity は pocket のもの: SES を見ない
        stacks.put(
            &domain.stack_name,
            FakeStacks::description("CREATE_COMPLETE", "h", &[]),
        );
        let aws = FakeAws::with(&[Some("SUCCESS")]);
        deploy(&stacks, &aws).prepare_deploy(&domain).await.unwrap();
        assert!(aws.calls.borrow().is_empty());
        assert!(stacks.writes().is_empty());
    }

    #[tokio::test]
    async fn test_create_update_and_delete() {
        let (domain, stacks, aws) = (domain(), FakeStacks::default(), FakeAws::default());
        assert!(domain.manage_dns);
        let (region, name) = (domain.region.clone(), domain.stack_name.clone());
        let deploy = deploy(&stacks, &aws);
        // stack が無ければ delete は何もしない
        deploy.delete(&domain).await.unwrap();
        assert!(stacks.writes().is_empty());
        deploy.create(&domain).await.unwrap();
        // 同じ template なら更新しない。hosted zone が変われば更新する
        deploy.update(&domain).await.unwrap();
        let mut moved = domain.clone();
        moved.hosted_zone_id_override = Some("Z1OTHER".to_string());
        deploy.update(&moved).await.unwrap();
        deploy.delete(&domain).await.unwrap();
        assert_eq!(
            stacks.writes(),
            [
                format!("create {region} {name}"),
                format!("update {region} {name}"),
                format!("delete {region} {name}"),
            ]
        );
        // 外部 DNS のドメインは hosted zone を探さない
        aws.calls.borrow_mut().clear();
        let mut external = domain.clone();
        external.manage_dns = false;
        deploy.create(&external).await.unwrap();
        assert!(aws.calls.borrow().is_empty());
    }

    #[tokio::test]
    async fn test_wait_verified_polls_until_success() {
        let (domain, stacks) = (domain(), FakeStacks::default());
        let aws = FakeAws::with(&[None, Some("PENDING"), Some("SUCCESS")]);
        let mut sink = Recorded::default();
        deploy(&stacks, &aws)
            .wait_verified(&domain, &mut sink)
            .await
            .unwrap();
        let logs: Vec<&str> = sink.0.iter().map(|(_, _, text)| text.as_str()).collect();
        assert_eq!(
            logs,
            [
                format!(
                    "Waiting for SES verification of {}... (status=None)",
                    domain.domain
                ),
                format!(
                    "Waiting for SES verification of {}... (status=PENDING)",
                    domain.domain
                ),
            ]
        );
    }

    #[tokio::test]
    async fn test_wait_verified_times_out() {
        let (domain, stacks) = (domain(), FakeStacks::default());
        let aws = FakeAws::with(&[Some("PENDING")]);
        let error = deploy(&stacks, &aws)
            .wait_verified(&domain, &mut Recorded::default())
            .await
            .unwrap_err()
            .to_string();
        assert_eq!(
            error,
            format!(
                "{} のSES検証が完了しません（status=PENDING）。zoneの委任を確認し、\
                 検証後に再度deployしてください。検証完了までは受信できません。\
                 状態は pocket resource inbound --stage <stage> --name <name> status で確認できます",
                domain.domain
            )
        );
        // 上限の回数 + 最後の 1 回だけ見る
        assert_eq!(aws.calls.borrow().len(), 4);
    }

    #[tokio::test]
    async fn test_confirm_waits_only_for_managed_dns() {
        let mut ctx = context();
        let stacks = FakeStacks::default();
        // pocket が DNS を持つドメイン: SUCCESS まで待つ
        let aws = FakeAws::with(&[Some("PENDING"), Some("SUCCESS")]);
        let mut sink = Recorded::default();
        deploy(&stacks, &aws)
            .confirm(&ctx, &mut sink)
            .await
            .unwrap();
        let domain = domain();
        let last = sink.0.last().unwrap();
        assert_eq!(
            (last.1, last.2.as_str()),
            (
                Style::Info,
                format!(
                    "inbound domain {}: verification=SUCCESS (waited 0s)",
                    domain.domain
                )
                .as_str()
            )
        );

        // 外部 DNS: 待たずに、登録すべき record を示す
        let mut external = magic_pocket_settings::types::OrderedMap::default();
        for (key, item) in ctx.inbound_domain.iter() {
            let mut item = item.clone();
            item.manage_dns = false;
            external.insert(key.clone(), item);
        }
        ctx.inbound_domain = external;
        stacks.put(
            &domain.stack_name,
            FakeStacks::description("CREATE_COMPLETE", "h", &dkim_outputs()),
        );
        let aws = FakeAws::with(&[Some("PENDING")]);
        let mut sink = Recorded::default();
        deploy(&stacks, &aws)
            .confirm(&ctx, &mut sink)
            .await
            .unwrap();
        assert_eq!(aws.calls.borrow().len(), 1);
        assert_eq!(
            (sink.0[0].1, sink.0[0].2.as_str()),
            (
                Style::Warning,
                format!(
                    "inbound domain {}: verification=PENDING (manage_dns = false のため待たない)。\
                     次のレコードをDNSに登録してください。検証完了までは受信できません。\
                     状態は pocket resource inbound --stage <stage> --name <name> status で確認できます",
                    domain.domain
                )
                .as_str()
            )
        );
        let records: Vec<&str> = sink.0[1..]
            .iter()
            .map(|(stream, _, text)| {
                assert_eq!(*stream, Stream::Stdout);
                text.as_str()
            })
            .collect();
        assert_eq!(
            records,
            [
                "  CNAME t1._domainkey.in.example.com t1.dkim.amazonses.com".to_string(),
                "  CNAME t2._domainkey.in.example.com t2.dkim.amazonses.com".to_string(),
                "  CNAME t3._domainkey.in.example.com t3.dkim.amazonses.com".to_string(),
                format!(
                    "  MX {} 10 inbound-smtp.ap-northeast-1.amazonaws.com",
                    domain.domain
                ),
            ]
        );

        // 外部 DNS で検証済み: 警告も record も出さない
        let aws = FakeAws::with(&[Some("SUCCESS")]);
        let mut sink = Recorded::default();
        deploy(&stacks, &aws)
            .confirm(&ctx, &mut sink)
            .await
            .unwrap();
        assert_eq!(
            sink.0
                .iter()
                .map(|(_, _, t)| t.as_str())
                .collect::<Vec<_>>(),
            [format!(
                "inbound domain {}: verification=SUCCESS (manage_dns = false)",
                domain.domain
            )]
        );
    }

    #[tokio::test]
    async fn test_cleanup_deletes_only_undeclared_domains_without_waiting() {
        let ctx = context();
        let declared = domain();
        let stacks = FakeStacks::default();
        let saved = json!({
            declared.domain.clone(): {"stack_name": declared.stack_name},
            "old.example.com": {"stack_name": "old-domain-stack"},
            "gone.example.com": {"stack_name": "gone-domain-stack"},
        });
        for name in [declared.stack_name.as_str(), "old-domain-stack"] {
            stacks.put(name, FakeStacks::description("CREATE_COMPLETE", "h", &[]));
        }
        // 削除の後も stack が残る (削除中) 状態にして、完了を待たないことを確かめる
        *stacks.after_write.borrow_mut() = Some("DELETE_IN_PROGRESS".to_string());
        let region = ctx.general.as_ref().unwrap().region.clone();
        let mut sink = Recorded::default();
        cleanup_unused_inbound_domains(&ctx, &saved, &stacks, &mut sink)
            .await
            .unwrap();
        // 宣言に残るドメインは消さない。stack が既に無いドメインには delete を出さない
        assert_eq!(
            stacks.writes(),
            [format!("delete {region} old-domain-stack")]
        );
        let calls = stacks.calls();
        let deleted_at = calls.iter().position(|c| c.starts_with("delete ")).unwrap();
        assert!(
            !calls[deleted_at..]
                .iter()
                .any(|call| call.starts_with("describe ") && call.ends_with("old-domain-stack")),
            "{calls:?}"
        );
        assert_eq!(
            sink.0
                .iter()
                .map(|(_, _, t)| t.as_str())
                .collect::<Vec<_>>(),
            ["Deleting unused inbound domain old.example.com..."]
        );
        // 記録が無い (初回の deploy)
        cleanup_unused_inbound_domains(&ctx, &Json::Null, &stacks, &mut sink)
            .await
            .unwrap();
        assert_eq!(stacks.writes().len(), 1);
    }

    #[test]
    fn test_dns_records_need_all_outputs() {
        let domain = domain();
        let mut outputs: StackOutputs = dkim_outputs()
            .into_iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        assert_eq!(dns_records(&domain, &outputs).unwrap().len(), 4);
        outputs.remove("DkimValue2");
        assert!(dns_records(&domain, &outputs)
            .unwrap_err()
            .to_string()
            .ends_with("の output に DkimValue2 がありません"));
    }
}
