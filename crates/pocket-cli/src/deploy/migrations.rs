//! リリースまたぎの移行の掃除 (Python 版 `pocket_cli/migrations.py` の cleanup)。
//!
//! 0.29.0 で container の設定が単数から複数になり、stack 名・ECR repository 名・
//! managed secret の置き場所が変わった。旧配置の値の引き継ぎは secret の用意
//! ([`super::mediator`]) が行い、ここは deploy 成功後の掃除を持つ:
//!
//! - 旧 project 共有パスに残った、container store へ移行済みの secret
//! - 旧形式の container stack (`{stage}-{project}-container`) と ECR repository
//!   (`{prefix}lambda`)。旧 stack の scheduler / SQS の消費が動き続けるので、放置は実害がある
//!
//! どれも確認してから消す (`-y` で承認)。移行済みの環境では何もしない

use std::collections::BTreeSet;

use anyhow::{Context as _, Result};
use magic_pocket_settings::context::{Context, SecretsContext};

use super::interaction::Prompt;
use super::secret_store::SecretStore;
use super::stacks::Stacks;
use crate::stack::ops::WaitOptions;
use crate::status::sink::Sink;

/// 旧 stack の削除の完了を待つ上限 (秒)。超えたら deploy を止めずに警告して先へ進む
const LEGACY_STACK_DELETE_TIMEOUT: u64 = 300;

/// ECR repository の有無と削除 (旧形式の repository の掃除と destroy が使う)
pub trait Repositories {
    async fn exists(&self, region: &str, name: &str) -> Result<bool>;
    async fn delete(&self, region: &str, name: &str) -> Result<()>;
}

/// `{stage}-{project}-{namespace}-` などの resource の prefix
pub fn resource_prefix(ctx: &Context) -> Result<String> {
    let general = ctx
        .general
        .as_ref()
        .context("general context is not configured")?;
    Ok(general
        .prefix_template
        .replace("{stage}", &ctx.stage)
        .replace("{project}", &ctx.project_name)
        .replace("{namespace}", &general.namespace))
}

/// 0.29.0 より前の container stack の名前 (今は `{stage}-{project}-container-{name}`)
pub fn legacy_container_stack_name(ctx: &Context) -> String {
    format!("{}-{}-container", ctx.stage, ctx.project_name)
}

/// 0.29.0 より前の ECR repository の名前 (今は `{prefix}{name}-lambda`)
pub fn legacy_ecr_repo_name(ctx: &Context) -> Result<String> {
    Ok(format!("{}lambda", resource_prefix(ctx)?))
}

/// 旧形式の container stack の状態名。無ければ `None`
pub async fn legacy_container_stack_status(
    ctx: &Context,
    stacks: &impl Stacks,
) -> Result<Option<String>> {
    let Some(general) = ctx.general.as_ref() else {
        return Ok(None);
    };
    let name = legacy_container_stack_name(ctx);
    let description = stacks.describe(&general.region, &name).await?;
    Ok(description
        .map(|d| d.status)
        .filter(|status| status != "DELETE_COMPLETE"))
}

/// 旧形式の ECR repository が在り、どの container もその名前を使っていないか
pub async fn legacy_ecr_repo_exists(ctx: &Context, repos: &impl Repositories) -> Result<bool> {
    let Some(general) = ctx.general.as_ref() else {
        return Ok(false);
    };
    let legacy = legacy_ecr_repo_name(ctx)?;
    if ctx.container.values().any(|c| c.ecr_name == legacy) {
        return Ok(false);
    }
    repos.exists(&general.region, &legacy).await
}

/// container store の view (container 名の昇順)
fn container_views(ctx: &Context) -> Vec<&SecretsContext> {
    let mut containers: Vec<_> = ctx.container.iter().collect();
    containers.sort_by(|a, b| a.0.cmp(b.0));
    containers
        .into_iter()
        .filter_map(|(_, c)| c.secrets.as_ref())
        .collect()
}

/// 旧 project 共有パスに残った、container store へ移行済みの secret を消す。
///
/// 消すのは「その key を宣言している全 container の store に値がある」key だけ
/// (一部の container だけコピー済みの段階で消すと、残りが引き継ぎ元を失う)。
/// shared 宣言の key は今も共有パスの住人なので残す
pub async fn cleanup_legacy_secret_residue(
    ctx: &Context,
    store: &impl SecretStore,
    prompt: &impl Prompt,
    sink: &mut dyn Sink,
) -> Result<()> {
    let Some(shared) = ctx.secrets.as_ref() else {
        return Ok(());
    };
    let shared_keys: BTreeSet<String> = store.read(shared).await?.keys().cloned().collect();
    if shared_keys.is_empty() {
        return Ok(());
    }
    let views = container_views(ctx);
    let mut stored = Vec::new();
    for view in &views {
        stored.push(store.read(view).await?);
    }
    let mut residue = BTreeSet::new();
    for key in &shared_keys {
        if shared.managed.contains_key(key) {
            continue;
        }
        let mut declaring = views
            .iter()
            .zip(&stored)
            .filter(|(view, _)| view.managed.contains_key(key))
            .peekable();
        if declaring.peek().is_none() {
            continue;
        }
        if declaring.all(|(_, secrets)| secrets.contains_key(key)) {
            residue.insert(key.clone());
        }
    }
    if residue.is_empty() {
        return Ok(());
    }
    let names: Vec<&str> = residue.iter().map(String::as_str).collect();
    sink.warning(&format!(
        "旧 project 共有パス ({}) に container store へ移行済みの secret が残っています: {}",
        shared.pocket_key,
        names.join(", ")
    ));
    if prompt.confirm("旧パス側の残骸を削除しますか？", true)? {
        store.delete_keys(shared, &residue).await?;
        sink.success("旧パスの secret 残骸を削除しました。");
    }
    Ok(())
}

/// DELETE_FAILED の stack から、失敗した resource と理由だけを取り出す
async fn failure_reasons(stacks: &impl Stacks, region: &str, name: &str) -> Result<String> {
    let lines: Vec<String> = stacks
        .resources(region, name)
        .await?
        .iter()
        .filter(|resource| resource.status.ends_with("FAILED"))
        .map(|resource| {
            format!(
                "  - {} ({}): {}",
                resource.logical_id,
                resource.resource_type,
                resource.status_reason.as_deref().unwrap_or("理由なし")
            )
        })
        .collect();
    Ok(if lines.is_empty() {
        "  (失敗したリソースの情報なし)".to_string()
    } else {
        lines.join("\n")
    })
}

/// 旧 stack を消して完了を待つ。失敗しても deploy は止めない (理由を出して、次の
/// deploy での再検出に任せる)
async fn delete_legacy_container_stack(
    stacks: &impl Stacks,
    region: &str,
    name: &str,
    wait: WaitOptions,
    sink: &mut dyn Sink,
) -> Result<()> {
    stacks.delete(region, name).await?;
    sink.log(&format!(
        "旧 stack の削除を開始しました (最大 {LEGACY_STACK_DELETE_TIMEOUT} 秒待ちます)。"
    ));
    for _ in 0..wait.attempts {
        tokio::time::sleep(wait.interval).await;
        let status = stacks.describe(region, name).await?.map(|d| d.status);
        match status.as_deref() {
            None | Some("DELETE_COMPLETE") => {
                sink.success(&format!("旧 stack '{name}' を削除しました。"));
                return Ok(());
            }
            Some("DELETE_FAILED") => {
                sink.danger(&format!(
                    "旧 stack '{name}' の削除が失敗しました (DELETE_FAILED)。deploy 自体は\
                     成功していますが、旧 stack の scheduler / SQS が残っている可能性が\
                     あります。失敗したリソース:\n{}\n\
                     CloudFormation の一過性エラーなら次回 deploy の再プロンプト、または\
                     次で再実行すると消えることがあります:\n  \
                     aws cloudformation delete-stack --stack-name {name}",
                    failure_reasons(stacks, region, name).await?
                ));
                return Ok(());
            }
            Some(_) => {}
        }
    }
    sink.warning(&format!(
        "旧 stack '{name}' の削除が {LEGACY_STACK_DELETE_TIMEOUT} 秒以内に終わりませんでした。\
         deploy はこのまま続けます。完了したか後で確認してください:\n  \
         aws cloudformation describe-stacks --stack-name {name}"
    ));
    Ok(())
}

/// 旧 stack の削除の完了待ち (10 秒おき)
pub fn legacy_stack_wait() -> WaitOptions {
    WaitOptions::new(LEGACY_STACK_DELETE_TIMEOUT, 10)
}

/// 旧形式の container stack と ECR repository を、確認してから消す
pub async fn cleanup_legacy_container_resources(
    ctx: &Context,
    stacks: &impl Stacks,
    repos: &impl Repositories,
    prompt: &impl Prompt,
    wait: WaitOptions,
    sink: &mut dyn Sink,
) -> Result<()> {
    let Some(general) = ctx.general.as_ref().filter(|_| !ctx.container.is_empty()) else {
        return Ok(());
    };
    let region = &general.region;
    let stack_name = legacy_container_stack_name(ctx);
    if let Some(status) = legacy_container_stack_status(ctx, stacks).await? {
        if status == "DELETE_FAILED" {
            sink.warning(&format!(
                "旧形式の container stack '{stack_name}' が前回の削除に失敗したまま\
                 残っています (DELETE_FAILED)。失敗したリソース:\n{}\n\
                 一過性のエラーなら削除の再実行で消えます。",
                failure_reasons(stacks, region, &stack_name).await?
            ));
        } else {
            sink.warning(&format!(
                "旧形式の container stack '{stack_name}' が残っています (0.29.0 の \
                 multi-container 化で stack 名が {{slug}}-container-{{name}} に\
                 変わりました)。旧 stack の scheduler / SQS が動き続けるため、\
                 削除を推奨します。"
            ));
        }
        if prompt.confirm(&format!("旧 stack '{stack_name}' を削除しますか？"), true)? {
            delete_legacy_container_stack(stacks, region, &stack_name, wait, sink).await?;
        }
    }
    if !legacy_ecr_repo_exists(ctx, repos).await? {
        return Ok(());
    }
    let repo = legacy_ecr_repo_name(ctx)?;
    sink.warning(&format!(
        "旧形式の ECR repository '{repo}' が残っています (新しい repo 名は \
         {{prefix}}{{container}}-lambda)。"
    ));
    if prompt.confirm(
        &format!("旧 ECR repository '{repo}' を削除しますか？"),
        true,
    )? {
        repos.delete(region, &repo).await?;
        sink.success("旧 ECR repository を削除しました。");
    }
    Ok(())
}

#[cfg(test)]
pub mod testing {
    use std::cell::RefCell;

    use super::*;

    /// 在る repository の名前を持つ fake
    #[derive(Default)]
    pub struct FakeRepositories {
        pub names: RefCell<BTreeSet<String>>,
        pub deleted: RefCell<Vec<String>>,
    }

    impl FakeRepositories {
        pub fn with(names: &[&str]) -> Self {
            Self {
                names: RefCell::new(names.iter().map(|n| n.to_string()).collect()),
                deleted: RefCell::default(),
            }
        }
    }

    impl Repositories for FakeRepositories {
        async fn exists(&self, _region: &str, name: &str) -> Result<bool> {
            Ok(self.names.borrow().contains(name))
        }

        async fn delete(&self, _region: &str, name: &str) -> Result<()> {
            self.names.borrow_mut().remove(name);
            self.deleted.borrow_mut().push(name.to_string());
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::testing::FakeRepositories;
    use super::*;
    use crate::deploy::interaction::testing::FakePrompt;
    use crate::deploy::poll::testing::fast;
    use crate::deploy::stacks::fake::FakeStacks;
    use crate::deploy::testing::FakeStore;
    use crate::stack::ops::StackResource;
    use crate::stack::testing::context_from_toml;
    use crate::status::sink::{Recorded, Style};

    const TOML: &str = r#"
[general]
region = "ap-southeast-1"
project_name = "testprj"
stages = ["prod"]

[container.web]
dockerfile_path = "Dockerfile"

[container.web.secrets.managed]
SECRET_KEY = { type = "password", shared = true }
WEB_ONLY = { type = "password" }
BOTH = { type = "password" }

[container.api]
dockerfile_path = "Dockerfile"

[container.api.secrets.managed]
SECRET_KEY = { type = "password", shared = true }
BOTH = { type = "password" }
"#;

    const SHARED: &str = "prod-testprj-pocket";
    const WEB: &str = "prod-testprj-web-pocket";
    const API: &str = "prod-testprj-api-pocket";
    const LEGACY_STACK: &str = "prod-testprj-container";
    const LEGACY_REPO: &str = "prod-testprj-pocket-lambda";

    fn context() -> Context {
        context_from_toml(TOML, "prod")
    }

    fn texts(sink: &Recorded, style: Style) -> Vec<&str> {
        sink.0
            .iter()
            .filter(|(_, s, _)| *s == style)
            .map(|(_, _, text)| text.as_str())
            .collect()
    }

    /// 旧共有パスに残っている key (名前順)
    fn sorted_keys(store: &FakeStore) -> Vec<String> {
        let mut keys = store.keys(SHARED);
        keys.sort();
        keys
    }

    #[test]
    fn test_legacy_names() {
        let ctx = context();
        assert_eq!(legacy_container_stack_name(&ctx), LEGACY_STACK);
        assert_eq!(legacy_ecr_repo_name(&ctx).unwrap(), LEGACY_REPO);
        assert_eq!(ctx.secrets.as_ref().unwrap().pocket_key, SHARED);
    }

    #[tokio::test]
    async fn test_secret_residue_is_deleted_only_when_every_declaring_container_copied() {
        let ctx = context();
        // 旧共有パス: 共有の現役 key・web だけの key・両方が宣言する key・宣言の無い key
        let shared = json!({"SECRET_KEY": "s", "WEB_ONLY": "w", "BOTH": "b", "UNDECLARED": "u"});
        // BOTH は web だけがコピー済み (api はまだ) → 消さない
        let store = FakeStore::default()
            .with(SHARED, shared.clone())
            .with(WEB, json!({"WEB_ONLY": "w", "BOTH": "b"}))
            .with(API, json!({}));
        let prompt = FakePrompt::yes();
        let mut sink = Recorded::default();
        cleanup_legacy_secret_residue(&ctx, &store, &prompt, &mut sink)
            .await
            .unwrap();
        assert_eq!(sorted_keys(&store), ["BOTH", "SECRET_KEY", "UNDECLARED"]);
        assert_eq!(
            texts(&sink, Style::Warning),
            [format!(
                "旧 project 共有パス ({SHARED}) に container store へ移行済みの secret が\
                 残っています: WEB_ONLY"
            )]
        );
        // api もコピーした後は BOTH も消す。shared 宣言の SECRET_KEY は残す
        let store = FakeStore::default()
            .with(SHARED, shared)
            .with(WEB, json!({"WEB_ONLY": "w", "BOTH": "b"}))
            .with(API, json!({"BOTH": "b"}));
        cleanup_legacy_secret_residue(&ctx, &store, &prompt, &mut Recorded::default())
            .await
            .unwrap();
        assert_eq!(sorted_keys(&store), ["SECRET_KEY", "UNDECLARED"]);
    }

    #[tokio::test]
    async fn test_secret_residue_is_kept_when_declined_or_absent() {
        let ctx = context();
        let store = FakeStore::default()
            .with(SHARED, json!({"SECRET_KEY": "s", "WEB_ONLY": "w"}))
            .with(WEB, json!({"WEB_ONLY": "w"}));
        let prompt = FakePrompt::answering(false);
        cleanup_legacy_secret_residue(&ctx, &store, &prompt, &mut Recorded::default())
            .await
            .unwrap();
        assert_eq!(
            prompt.asked.borrow().as_slice(),
            ["旧パス側の残骸を削除しますか？"]
        );
        assert_eq!(sorted_keys(&store), ["SECRET_KEY", "WEB_ONLY"]);
        // 移行済み (残りが無い) の環境では聞かない
        let store = FakeStore::default()
            .with(SHARED, json!({"SECRET_KEY": "s"}))
            .with(WEB, json!({"WEB_ONLY": "w"}));
        let prompt = FakePrompt::answering(true);
        let mut sink = Recorded::default();
        cleanup_legacy_secret_residue(&ctx, &store, &prompt, &mut sink)
            .await
            .unwrap();
        assert!(prompt.asked.borrow().is_empty());
        assert!(sink.0.is_empty());
        assert!(store.calls.borrow().is_empty());
    }

    #[tokio::test]
    async fn test_legacy_stack_and_repository_are_deleted_after_confirmation() {
        let ctx = context();
        let stacks = FakeStacks::default();
        stacks.put(
            LEGACY_STACK,
            FakeStacks::description("UPDATE_COMPLETE", "h", &[]),
        );
        // 新旧が混在: 今の container の stack は消さない
        stacks.put(
            "prod-testprj-container-web",
            FakeStacks::description("UPDATE_COMPLETE", "h", &[]),
        );
        let repos = FakeRepositories::with(&[LEGACY_REPO, "prod-testprj-pocket-web-lambda"]);
        let prompt = FakePrompt::yes();
        let mut sink = Recorded::default();
        cleanup_legacy_container_resources(&ctx, &stacks, &repos, &prompt, fast(3), &mut sink)
            .await
            .unwrap();
        assert_eq!(
            stacks.writes(),
            [format!("delete ap-southeast-1 {LEGACY_STACK}")]
        );
        assert_eq!(repos.deleted.borrow().as_slice(), [LEGACY_REPO]);
        assert_eq!(
            texts(&sink, Style::Success),
            [
                format!("旧 stack '{LEGACY_STACK}' を削除しました。").as_str(),
                "旧 ECR repository を削除しました。"
            ]
        );
    }

    #[tokio::test]
    async fn test_legacy_resources_are_kept_when_declined() {
        let ctx = context();
        let stacks = FakeStacks::default();
        stacks.put(
            LEGACY_STACK,
            FakeStacks::description("UPDATE_COMPLETE", "h", &[]),
        );
        let repos = FakeRepositories::with(&[LEGACY_REPO]);
        let prompt = FakePrompt::answering(false);
        cleanup_legacy_container_resources(
            &ctx,
            &stacks,
            &repos,
            &prompt,
            fast(3),
            &mut Recorded::default(),
        )
        .await
        .unwrap();
        assert_eq!(
            prompt.asked.borrow().as_slice(),
            [
                format!("旧 stack '{LEGACY_STACK}' を削除しますか？"),
                format!("旧 ECR repository '{LEGACY_REPO}' を削除しますか？"),
            ]
        );
        assert!(stacks.writes().is_empty());
        assert!(repos.deleted.borrow().is_empty());
    }

    #[tokio::test]
    async fn test_migrated_environment_is_untouched() {
        let ctx = context();
        let stacks = FakeStacks::default();
        let repos = FakeRepositories::with(&["prod-testprj-pocket-web-lambda"]);
        let prompt = FakePrompt::answering(true);
        let mut sink = Recorded::default();
        cleanup_legacy_container_resources(&ctx, &stacks, &repos, &prompt, fast(3), &mut sink)
            .await
            .unwrap();
        assert!(prompt.asked.borrow().is_empty());
        assert!(sink.0.is_empty());
        assert!(stacks.writes().is_empty());
    }

    #[tokio::test]
    async fn test_failed_legacy_delete_warns_and_continues() {
        let ctx = context();
        let stacks = FakeStacks::default();
        stacks.put(
            LEGACY_STACK,
            FakeStacks::description("UPDATE_COMPLETE", "h", &[]),
        );
        *stacks.after_write.borrow_mut() = Some("DELETE_FAILED".to_string());
        *stacks.resources.borrow_mut() = vec![StackResource {
            logical_id: "WsgiLambda".to_string(),
            resource_type: "AWS::Lambda::Function".to_string(),
            physical_id: None,
            status: "DELETE_FAILED".to_string(),
            status_reason: Some("Rate exceeded".to_string()),
        }];
        let repos = FakeRepositories::default();
        let mut sink = Recorded::default();
        cleanup_legacy_container_resources(
            &ctx,
            &stacks,
            &repos,
            &FakePrompt::yes(),
            fast(3),
            &mut sink,
        )
        .await
        .unwrap();
        let danger = texts(&sink, Style::Danger);
        assert_eq!(danger.len(), 1);
        assert!(danger[0].contains("  - WsgiLambda (AWS::Lambda::Function): Rate exceeded"));
        // 次の deploy では、失敗したまま残っていることを知らせてもう一度聞く
        let mut sink = Recorded::default();
        let prompt = FakePrompt::answering(false);
        cleanup_legacy_container_resources(&ctx, &stacks, &repos, &prompt, fast(3), &mut sink)
            .await
            .unwrap();
        assert!(texts(&sink, Style::Warning)[0].contains("前回の削除に失敗したまま"));
        assert_eq!(prompt.asked.borrow().len(), 1);
    }

    #[tokio::test]
    async fn test_slow_legacy_delete_times_out_without_failing() {
        let ctx = context();
        let stacks = FakeStacks::default();
        stacks.put(
            LEGACY_STACK,
            FakeStacks::description("UPDATE_COMPLETE", "h", &[]),
        );
        *stacks.after_write.borrow_mut() = Some("DELETE_IN_PROGRESS".to_string());
        let mut sink = Recorded::default();
        cleanup_legacy_container_resources(
            &ctx,
            &stacks,
            &FakeRepositories::default(),
            &FakePrompt::yes(),
            fast(2),
            &mut sink,
        )
        .await
        .unwrap();
        assert!(texts(&sink, Style::Warning)
            .last()
            .unwrap()
            .contains("300 秒以内に終わりませんでした"));
    }
}
