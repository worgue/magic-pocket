//! CloudFront の前に作る stack (ACM 証明書 / WAF / 署名用の鍵) の手順。Python 版
//! `resources/cloudfront_acm.py` / `cloudfront_waf.py` / `cloudfront_keys.py`。
//!
//! どれも stack 1 つに対応する: create は作成して完了を待ち、update は template が
//! 変わっているときだけ更新して待つ。WAF と鍵は template に managed secret の値が
//! 入るので、先に secret を用意する。

use std::collections::BTreeMap;

use anyhow::{bail, Context as _, Result};
use magic_pocket_settings::context::{CloudFrontContext, CloudFrontWafRateLimitContext, Context};

use super::mediator::{Mediator, SecretsReady};
use super::secret_store::SecretStore;
use super::stacks::{self, Stacks};
use super::template_secrets;
use crate::stack::cloudfront as cf_stack;
use crate::stack::ops::{template_synced, ResourceStatus, WaitOptions};
use crate::stack::StackTemplate;
use crate::status::sink::Sink;

/// ACM / WAF の stack を置く region (CloudFront が参照できるのはここだけ)
pub const EDGE_REGION: &str = "us-east-1";

/// stack の完了待ち (Python 版の `wait_timeout` / `wait_interval`)
#[derive(Debug, Clone, Copy)]
pub struct EdgeWaits {
    /// DNS 検証があるので長い
    pub acm: WaitOptions,
    pub waf: WaitOptions,
    pub keys: WaitOptions,
    pub delete: WaitOptions,
}

impl Default for EdgeWaits {
    fn default() -> Self {
        Self {
            acm: WaitOptions::new(600, 10),
            waf: WaitOptions::new(300, 10),
            keys: WaitOptions::new(120, 5),
            delete: WaitOptions::new(300, 10),
        }
    }
}

pub struct EdgeDeploy<'a, K, S> {
    pub stacks: &'a K,
    pub store: &'a S,
    pub waits: EdgeWaits,
}

/// rate rule を 1 行で表す (Python の `format_rate_limit`)
pub fn format_rate_limit(name: &str, rule: &CloudFrontWafRateLimitContext) -> String {
    let action = match serde_json::to_value(rule.action) {
        Ok(serde_json::Value::String(action)) => action,
        _ => String::new(),
    };
    format!(
        "{name}: {} req / {} 秒, path={}, action={action}",
        rule.limit,
        rule.window_sec,
        rule.path
            .as_deref()
            .filter(|path| !path.is_empty())
            .unwrap_or("(全リクエスト)"),
    )
}

pub fn find_cloudfront<'c>(ctx: &'c Context, name: &str) -> Result<&'c CloudFrontContext> {
    ctx.cloudfront
        .get(name)
        .with_context(|| format!("cloudfront '{name}' がこの stage に設定されていません。"))
}

impl<K: Stacks, S: SecretStore> EdgeDeploy<'_, K, S> {
    async fn create_stack(
        &self,
        stack: &StackTemplate,
        label: &str,
        wait: WaitOptions,
    ) -> Result<()> {
        self.stacks.create(stack).await?;
        stacks::wait(
            self.stacks,
            &stack.region,
            &stack.name,
            label,
            ResourceStatus::Completed,
            wait,
        )
        .await
    }

    /// template が変わっているときだけ更新して待つ
    async fn update_stack(
        &self,
        stack: &StackTemplate,
        label: &str,
        wait: WaitOptions,
    ) -> Result<()> {
        let description = self.stacks.describe(&stack.region, &stack.name).await?;
        if template_synced(description.as_ref(), stack)? {
            return Ok(());
        }
        self.stacks.update(stack).await?;
        stacks::wait(
            self.stacks,
            &stack.region,
            &stack.name,
            label,
            ResourceStatus::Completed,
            wait,
        )
        .await
    }

    async fn delete_stack(&self, region: &str, name: &str, label: &str) -> Result<()> {
        self.stacks.delete(region, name).await?;
        stacks::wait(
            self.stacks,
            region,
            name,
            label,
            ResourceStatus::NoExist,
            self.waits.delete,
        )
        .await
    }

    fn mediator<'m>(&'m self, ctx: &'m Context) -> Mediator<'m, S> {
        Mediator {
            ctx,
            store: self.store,
        }
    }

    // ---- ACM ----

    pub async fn acm_create(&self, cf: &CloudFrontContext, sink: &mut dyn Sink) -> Result<()> {
        sink.log("ACM 証明書を作成中（DNS 検証の完了まで数分かかります）...");
        self.create_stack(&cf_stack::acm_stack(cf)?, "cloudfront_acm", self.waits.acm)
            .await
    }

    pub async fn acm_update(&self, cf: &CloudFrontContext) -> Result<()> {
        self.update_stack(&cf_stack::acm_stack(cf)?, "cloudfront_acm", self.waits.acm)
            .await
    }

    pub async fn acm_delete(&self, cf: &CloudFrontContext, sink: &mut dyn Sink) -> Result<()> {
        sink.log("ACM スタックを削除中...");
        self.delete_stack(EDGE_REGION, &cf_stack::acm_stack_name(cf), "cloudfront_acm")
            .await
    }

    // ---- WAF ----

    /// allow rule の header secret を用意して読む。deploy の順は WAF → container なので、
    /// 初回はここで生成する
    async fn waf_stack(
        &self,
        ctx: &Context,
        name: &str,
        sink: &mut dyn Sink,
        reload: impl FnOnce() -> Result<Context>,
    ) -> Result<(Context, StackTemplate)> {
        let keys: Vec<String> = find_cloudfront(ctx, name)?
            .waf
            .as_ref()
            .map(|waf| waf.header_secret_keys.clone())
            .unwrap_or_default();
        if keys.is_empty() {
            let stack = cf_stack::waf_stack(find_cloudfront(ctx, name)?, &BTreeMap::new())?;
            return Ok((ctx.clone(), stack));
        }
        let SecretsReady(ctx) = self
            .mediator(ctx)
            .ensure_pocket_managed_secrets(sink, reload)
            .await?;
        let values = match ctx.secrets.as_ref() {
            // settings の検査を通っていれば到達しない
            None => {
                sink.warning("container secrets is not configured.");
                BTreeMap::new()
            }
            Some(view) => {
                template_secrets::waf_allow_secret_values(&keys, &self.store.read(view).await?)
            }
        };
        let missing: Vec<&str> = keys
            .iter()
            .filter(|key| !values.contains_key(*key))
            .map(String::as_str)
            .collect();
        if !missing.is_empty() {
            bail!(
                "waf allow secret が managed secrets に見つかりません: {}",
                missing.join(", ")
            );
        }
        let stack = cf_stack::waf_stack(find_cloudfront(&ctx, name)?, &values)?;
        Ok((ctx, stack))
    }

    pub async fn waf_create(
        &self,
        ctx: &Context,
        name: &str,
        sink: &mut dyn Sink,
        reload: impl FnOnce() -> Result<Context>,
    ) -> Result<()> {
        sink.log("WAF (IPSet + WebACL) を作成中 (us-east-1)...");
        let (ctx, stack) = self.waf_stack(ctx, name, sink, reload).await?;
        let cf = find_cloudfront(&ctx, name)?;
        echo_rate_limits(cf, sink);
        echo_allow_rules(cf, sink);
        self.create_stack(&stack, "cloudfront_waf", self.waits.waf)
            .await?;
        let waf = cf.waf.as_ref().context("waf context is not configured")?;
        if waf.enable_ip_set {
            sink.info(&format!(
                "WAF を作成しました。`pocket waf ip add self --name {} --stage {}` \
                 で自分の IP を allowlist に追加してください \
                 (空の状態では deny-all になります)。",
                cf.name, cf.stage
            ));
        } else {
            sink.info(&format!(
                "WAF を作成しました (IP allowlist 無効。rate_limits {} 件、\
                 managed rules {} 件)。",
                waf.rate_limits.len(),
                waf.managed_rule_groups.len()
            ));
        }
        Ok(())
    }

    pub async fn waf_update(
        &self,
        ctx: &Context,
        name: &str,
        sink: &mut dyn Sink,
        reload: impl FnOnce() -> Result<Context>,
    ) -> Result<()> {
        let (ctx, stack) = self.waf_stack(ctx, name, sink, reload).await?;
        let cf = find_cloudfront(&ctx, name)?;
        echo_rate_limits(cf, sink);
        echo_allow_rules(cf, sink);
        self.update_stack(&stack, "cloudfront_waf", self.waits.waf)
            .await
    }

    pub async fn waf_delete(&self, cf: &CloudFrontContext, sink: &mut dyn Sink) -> Result<()> {
        sink.log("WAF スタックを削除中...");
        self.delete_stack(EDGE_REGION, &cf_stack::waf_stack_name(cf), "cloudfront_waf")
            .await
    }

    // ---- 署名用の鍵 ----

    /// managed secret を用意して、公開鍵を入れた template を組む
    async fn keys_stack(
        &self,
        ctx: &Context,
        name: &str,
        sink: &mut dyn Sink,
        reload: impl FnOnce() -> Result<Context>,
    ) -> Result<StackTemplate> {
        let SecretsReady(ctx) = self
            .mediator(ctx)
            .ensure_pocket_managed_secrets(sink, reload)
            .await?;
        let cf = find_cloudfront(&ctx, name)?;
        let pem = match cf.signing_key.as_deref().filter(|key| !key.is_empty()) {
            None => String::new(),
            Some(key) => match template_secrets::managed_secret_view(&ctx, key) {
                None => {
                    sink.warning("container secrets is not configured.");
                    String::new()
                }
                Some(view) => {
                    let secrets = self.store.read(view).await?;
                    template_secrets::signing_public_key_pem(key, &secrets, sink)?
                }
            },
        };
        cf_stack::keys_stack(cf, &pem)
    }

    pub async fn keys_create(
        &self,
        ctx: &Context,
        name: &str,
        sink: &mut dyn Sink,
        reload: impl FnOnce() -> Result<Context>,
    ) -> Result<()> {
        let stack = self.keys_stack(ctx, name, sink, reload).await?;
        self.create_stack(&stack, "cloudfront_keys", self.waits.keys)
            .await
    }

    pub async fn keys_update(
        &self,
        ctx: &Context,
        name: &str,
        sink: &mut dyn Sink,
        reload: impl FnOnce() -> Result<Context>,
    ) -> Result<()> {
        let stack = self.keys_stack(ctx, name, sink, reload).await?;
        self.update_stack(&stack, "cloudfront_keys", self.waits.keys)
            .await
    }

    pub async fn keys_delete(&self, cf: &CloudFrontContext, sink: &mut dyn Sink) -> Result<()> {
        sink.info("Deleting CloudFront keys stack ...");
        self.delete_stack(
            &cf.region,
            &cf_stack::keys_stack_name(cf),
            "cloudfront_keys",
        )
        .await
    }
}

/// rate_limits は全ルールの先頭で効くので、deploy のたびに一覧を見せる
fn echo_rate_limits(cf: &CloudFrontContext, sink: &mut dyn Sink) {
    let Some(waf) = cf.waf.as_ref().filter(|waf| !waf.rate_limits.is_empty()) else {
        return;
    };
    sink.info(&format!(
        "WAF rate_limits ({} 件、全ルールの先頭で評価。超過した送信元 IP は \
         allow_rules / allowlist 済みでも対象):",
        waf.rate_limits.len()
    ));
    for (i, rule) in waf.rate_limits.iter().enumerate() {
        sink.info(&format!(
            "  {}",
            format_rate_limit(&format!("ratelimit-{i}"), rule)
        ));
    }
}

/// allow_rules は WAF を弱める宣言なので、deploy のたびに一覧を見せる
fn echo_allow_rules(cf: &CloudFrontContext, sink: &mut dyn Sink) {
    let Some(waf) = cf.waf.as_ref().filter(|waf| !waf.allow_rules.is_empty()) else {
        return;
    };
    sink.info(&format!(
        "WAF allow_rules ({} 件、IPSet / managed rules より先に評価):",
        waf.allow_rules.len()
    ));
    for (i, rule) in waf.allow_rules.iter().enumerate() {
        let mut parts = Vec::new();
        if let Some(path) = rule.path.as_deref().filter(|path| !path.is_empty()) {
            parts.push(format!("path={path}"));
        }
        if let Some(header) = rule.header.as_deref().filter(|header| !header.is_empty()) {
            parts.push(format!("header secret={header}"));
        }
        sink.info(&format!("  allow-{i}: {}", parts.join(" AND ")));
    }
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use base64::Engine as _;

    use super::*;
    use crate::deploy::stacks::fake::FakeStacks;
    use crate::deploy::testing::FakeStore;
    use crate::stack::testing::vector_context;
    use crate::status::sink::{Recorded, Style};

    const VECTOR: &str = "toml_vectors/cloudfront_full.dev";

    fn run<T>(future: impl std::future::Future<Output = T>) -> T {
        tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .expect("runtime")
            .block_on(future)
    }

    fn fast() -> EdgeWaits {
        let wait = WaitOptions {
            attempts: 3,
            interval: Duration::ZERO,
            timeout_secs: 0,
        };
        EdgeWaits {
            acm: wait,
            waf: wait,
            keys: wait,
            delete: wait,
        }
    }

    fn edge<'a>(
        stacks: &'a FakeStacks,
        store: &'a FakeStore,
    ) -> EdgeDeploy<'a, FakeStacks, FakeStore> {
        EdgeDeploy {
            stacks,
            store,
            waits: fast(),
        }
    }

    fn infos(sink: &Recorded) -> Vec<&str> {
        sink.0
            .iter()
            .filter(|record| record.1 == Style::Info)
            .map(|record| record.2.as_str())
            .collect()
    }

    #[test]
    fn test_default_waits_match_python() {
        let waits = EdgeWaits::default();
        assert_eq!((waits.acm.attempts, waits.acm.interval.as_secs()), (60, 10));
        assert_eq!((waits.waf.attempts, waits.waf.interval.as_secs()), (30, 10));
        assert_eq!(
            (waits.keys.attempts, waits.keys.interval.as_secs()),
            (24, 5)
        );
        assert_eq!(
            (waits.delete.attempts, waits.delete.interval.as_secs()),
            (30, 10)
        );
    }

    #[test]
    fn test_acm_create_then_update_is_noop_when_synced() {
        let ctx = vector_context(VECTOR);
        let cf = find_cloudfront(&ctx, "web").expect("web");
        let (stacks, store) = (FakeStacks::default(), FakeStore::default());
        let edge = edge(&stacks, &store);
        let mut sink = Recorded::default();
        run(edge.acm_create(cf, &mut sink)).expect("create");
        let name = cf_stack::acm_stack_name(cf);
        assert_eq!(stacks.writes(), [format!("create us-east-1 {name}")]);
        run(edge.acm_update(cf)).expect("update");
        assert_eq!(stacks.writes().len(), 1, "template が同じなら更新しない");
        // deploy 済みの hash が違えば更新する
        stacks.put(
            &name,
            FakeStacks::description("UPDATE_COMPLETE", "0000000000000000", &[]),
        );
        run(edge.acm_update(cf)).expect("update");
        assert_eq!(stacks.writes()[1], format!("update us-east-1 {name}"));
    }

    #[test]
    fn test_create_fails_when_stack_fails() {
        let ctx = vector_context(VECTOR);
        let cf = find_cloudfront(&ctx, "web").expect("web");
        let (stacks, store) = (FakeStacks::default(), FakeStore::default());
        *stacks.after_write.borrow_mut() = Some("CREATE_FAILED".to_string());
        let mut sink = Recorded::default();
        let error = run(edge(&stacks, &store).acm_create(cf, &mut sink)).expect_err("失敗");
        assert!(error.to_string().contains("CREATE_FAILED"), "{error}");
    }

    #[test]
    fn test_delete_waits_until_stack_is_gone() {
        let ctx = vector_context(VECTOR);
        let cf = find_cloudfront(&ctx, "web").expect("web");
        let (stacks, store) = (FakeStacks::default(), FakeStore::default());
        let edge = edge(&stacks, &store);
        let mut sink = Recorded::default();
        for (name, region) in [
            (cf_stack::acm_stack_name(cf), "us-east-1"),
            (cf_stack::waf_stack_name(cf), "us-east-1"),
            (cf_stack::keys_stack_name(cf), cf.region.as_str()),
        ] {
            stacks.put(&name, FakeStacks::description("CREATE_COMPLETE", "x", &[]));
            let _ = region;
        }
        run(edge.acm_delete(cf, &mut sink)).expect("acm");
        run(edge.waf_delete(cf, &mut sink)).expect("waf");
        run(edge.keys_delete(cf, &mut sink)).expect("keys");
        assert_eq!(
            stacks.writes(),
            [
                format!("delete us-east-1 {}", cf_stack::acm_stack_name(cf)),
                format!("delete us-east-1 {}", cf_stack::waf_stack_name(cf)),
                format!("delete {} {}", cf.region, cf_stack::keys_stack_name(cf)),
            ]
        );
        // 消えないまま待機が尽きたら止める
        let name = cf_stack::acm_stack_name(cf);
        stacks.put(&name, FakeStacks::description("CREATE_COMPLETE", "x", &[]));
        *stacks.after_write.borrow_mut() = Some("DELETE_IN_PROGRESS".to_string());
        let error = run(edge.acm_delete(cf, &mut sink)).expect_err("timeout");
        assert!(error.to_string().contains("タイムアウト"), "{error}");
    }

    #[test]
    fn test_waf_create_generates_secrets_before_stack() {
        let ctx = vector_context(VECTOR);
        let (stacks, store) = (FakeStacks::default(), FakeStore::default());
        let mut sink = Recorded::default();
        run(edge(&stacks, &store).waf_create(&ctx, "web", &mut sink, || Ok(ctx.clone())))
            .expect("create");
        // header secret は shared store に生成されてから stack が作られる
        let shared = ctx.secrets.as_ref().expect("shared");
        let stored = store.secrets(&shared.pocket_key);
        assert!(stored.contains_key("SMOKE_ALLOW") && stored.contains_key("HOOK_ALLOW"));
        let cf = find_cloudfront(&ctx, "web").expect("web");
        assert_eq!(
            stacks.writes(),
            [format!("create us-east-1 {}", cf_stack::waf_stack_name(cf))]
        );
        let infos = infos(&sink);
        assert!(infos
            .iter()
            .any(|line| line.starts_with("WAF rate_limits (2 件")));
        assert!(
            infos.contains(&"  ratelimit-0: 100 req / 300 秒, path=(全リクエスト), action=block")
        );
        assert!(infos.contains(&"  ratelimit-1: 2000 req / 60 秒, path=/api/*, action=count"));
        assert!(infos.contains(&"  allow-0: path=/healthz"));
        assert!(infos.contains(&"  allow-2: path=/api/hook* AND header secret=HOOK_ALLOW"));
        assert!(infos
            .last()
            .is_some_and(|line| line.contains("pocket waf ip add self --name web --stage dev")));
    }

    #[test]
    fn test_waf_without_ip_set_and_header_secrets_skips_mediator() {
        let ctx = vector_context(VECTOR);
        let (stacks, store) = (FakeStacks::default(), FakeStore::default());
        let mut sink = Recorded::default();
        run(
            edge(&stacks, &store).waf_create(&ctx, "admin", &mut sink, || {
                panic!("header secret が無ければ secret を用意しない")
            }),
        )
        .expect("create");
        assert!(store.calls.borrow().is_empty());
        assert_eq!(
            infos(&sink).last().copied(),
            Some("WAF を作成しました (IP allowlist 無効。rate_limits 1 件、managed rules 0 件)。")
        );
    }

    #[test]
    fn test_waf_update_only_when_template_changes() {
        let ctx = vector_context(VECTOR);
        let (stacks, store) = (FakeStacks::default(), FakeStore::default());
        let edge = edge(&stacks, &store);
        let mut sink = Recorded::default();
        run(edge.waf_create(&ctx, "web", &mut sink, || Ok(ctx.clone()))).expect("create");
        run(edge.waf_update(&ctx, "web", &mut sink, || Ok(ctx.clone()))).expect("update");
        assert_eq!(
            stacks.writes().len(),
            1,
            "secret が同じなら template も同じ"
        );
        // secret の値が変わると template が変わるので更新する
        let shared = ctx.secrets.as_ref().expect("shared");
        store
            .managed
            .borrow_mut()
            .get_mut(&shared.pocket_key)
            .expect("stored")
            .insert("SMOKE_ALLOW".to_string(), "rotated".into());
        run(edge.waf_update(&ctx, "web", &mut sink, || Ok(ctx.clone()))).expect("update");
        assert!(stacks.writes()[1].starts_with("update us-east-1 "));
    }

    #[test]
    fn test_keys_create_reads_generated_public_key() {
        let ctx = vector_context(VECTOR);
        let (stacks, store) = (FakeStacks::default(), FakeStore::default());
        let edge = edge(&stacks, &store);
        let mut sink = Recorded::default();
        run(edge.keys_create(&ctx, "web", &mut sink, || Ok(ctx.clone()))).expect("create");
        let cf = find_cloudfront(&ctx, "web").expect("web");
        let view = template_secrets::managed_secret_view(&ctx, "SIGNING_KEY").expect("view");
        let public = store.secrets(&view.pocket_key)["SIGNING_KEY"]["pub"]
            .as_str()
            .expect("pub")
            .to_string();
        let pem = String::from_utf8(
            base64::engine::general_purpose::STANDARD
                .decode(public)
                .expect("base64"),
        )
        .expect("utf8");
        assert!(pem.starts_with("-----BEGIN PUBLIC KEY-----"));
        // 作った stack は、生成された公開鍵を入れた template と一致する
        let want = cf_stack::keys_stack(cf, &pem).expect("stack");
        let description = run(stacks.describe(&cf.region, &want.name)).expect("describe");
        assert!(template_synced(description.as_ref(), &want).expect("synced"));
        run(edge.keys_update(&ctx, "web", &mut sink, || Ok(ctx.clone()))).expect("update");
        assert_eq!(
            stacks.writes(),
            [format!("create {} {}", cf.region, want.name)]
        );
    }
}
