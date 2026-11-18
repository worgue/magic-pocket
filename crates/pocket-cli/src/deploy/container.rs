//! container の deploy の手順 (Python 版 `pocket_cli/resources/container.py` の
//! deploy_init / create / update)。
//!
//! AWS の呼び出しは [`ContainerAws`] / [`LambdaApi`] / [`SecretStore`] の裏に置き、手順の
//! 順序と分岐を fake でテストする。

use anyhow::{bail, Context as _, Result};
use magic_pocket_settings::context::{ContainerContext, Context};
use serde_json::Value as Json;

use super::lambda::{self, LambdaApi, WaitUpdate};
use super::mediator::{DatabaseUrls, Mediator, SecretsReady};
use super::secret_store::SecretStore;
use crate::stack::ops::{
    cfn_status, template_synced, wait_status, ResourceStatus, StackApi, StackDescription,
    StackResource, WaitOptions,
};
use crate::stack::{vpc as vpc_stack, StackTemplate};
use crate::status::sink::Sink;

/// 外部 VPC の stack が共有を許可していることを示すタグ
const SHARABLE_TAG: &str = "pocket:sharable";

/// container の deploy が使う AWS の操作 (Lambda と secret store を除く)
pub trait ContainerAws {
    async fn describe_stack(&self, region: &str, name: &str) -> Result<Option<StackDescription>>;
    async fn stack_resources(&self, region: &str, name: &str) -> Result<Vec<StackResource>>;
    /// deploy 済みの template のテキスト。stack が無ければ `None`
    async fn uploaded_template(&self, region: &str, name: &str) -> Result<Option<String>>;
    async fn create_stack(&self, stack: &StackTemplate) -> Result<()>;
    async fn update_stack(&self, stack: &StackTemplate) -> Result<()>;
    /// 外部 VPC の stack に、この project が使っていることを示すタグを付ける
    async fn add_consumer_tag(&self, region: &str, stack_arn: &str, slug: &str) -> Result<()>;
    /// container の stack の template を描く (RDS / DSQL / VPC の実体を引く)
    async fn container_template(
        &self,
        ctx: &Context,
        c: &ContainerContext,
    ) -> Result<StackTemplate>;
    /// ECR repository の URI。無ければ `None`
    async fn repository_uri(&self, c: &ContainerContext) -> Result<Option<String>>;
    /// image の用意 (昇格なら tag の付け替え、そうでなければ build & push)
    async fn sync_image(&self, c: &ContainerContext) -> Result<()>;
}

/// 待機の間隔と回数
#[derive(Debug, Clone, Copy)]
pub struct Waits {
    /// managed VPC の stack の完了待ち
    pub vpc: WaitOptions,
    /// container の stack の作成・更新の完了待ち (Python 版は 600 秒を 10 秒おき)
    pub stack: WaitOptions,
    pub lambda: WaitUpdate,
}

impl Default for Waits {
    fn default() -> Self {
        Self {
            vpc: WaitOptions::default(),
            stack: WaitOptions::new(600, 10),
            lambda: WaitUpdate::default(),
        }
    }
}

pub struct ContainerDeploy<'a, A, L, S, D> {
    pub aws: &'a A,
    pub lambda: &'a L,
    pub store: &'a S,
    pub urls: &'a D,
    pub waits: Waits,
}

/// region を固定した stack の読み取り (`wait_status` に渡す)
struct RegionStacks<'a, A> {
    aws: &'a A,
    region: &'a str,
}

impl<A: ContainerAws> StackApi for RegionStacks<'_, A> {
    async fn describe(&self, name: &str) -> Result<Option<StackDescription>> {
        self.aws.describe_stack(self.region, name).await
    }

    async fn resources(&self, name: &str) -> Result<Vec<StackResource>> {
        self.aws.stack_resources(self.region, name).await
    }
}

impl<A: ContainerAws, L: LambdaApi, S: SecretStore, D: DatabaseUrls>
    ContainerDeploy<'_, A, L, S, D>
{
    /// stack を作る・更新する前の用意: image と、外部 VPC の検査
    pub async fn deploy_init(&self, c: &ContainerContext) -> Result<()> {
        self.aws.sync_image(c).await?;
        let Some(vpc) = c.vpc.as_ref().filter(|vpc| !vpc.manage) else {
            return Ok(());
        };
        let name = vpc_stack::stack_name(vpc);
        let description = self.aws.describe_stack(&vpc.region, &name).await?;
        let Some(description) = description
            .as_ref()
            .filter(|d| cfn_status(Some(d)).is_ok_and(|s| s != ResourceStatus::NoExist))
        else {
            bail!("外部 VPC スタック '{name}' が見つかりません。");
        };
        let sharable = description
            .tags
            .iter()
            .any(|(key, value)| key == SHARABLE_TAG && value == "true");
        if !sharable {
            bail!("VPC '{name}' は共有が許可されていません (sharable = true が必要です)");
        }
        self.aws
            .add_consumer_tag(&vpc.region, &description.stack_id, &c.slug)
            .await
    }

    /// stack が無い container を作る。`reload` は secret の用意の後に Context を作り直す
    pub async fn create(
        &self,
        ctx: &Context,
        container: &str,
        sink: &mut dyn Sink,
        reload: impl FnOnce() -> Result<Context>,
    ) -> Result<()> {
        let c = find(ctx, container)?;
        if let Some(vpc) = c.vpc.as_ref().filter(|vpc| vpc.manage) {
            let stacks = RegionStacks {
                aws: self.aws,
                region: &vpc.region,
            };
            wait_status(
                &stacks,
                &vpc_stack::stack_name(vpc),
                "vpc",
                ResourceStatus::Completed,
                self.waits.vpc,
            )
            .await?;
        }
        sink.print("Creating secrets ...");
        let SecretsReady(ctx) = self
            .mediator(ctx)
            .ensure_pocket_managed_secrets(sink, reload)
            .await?;
        let c = find(&ctx, container)?;
        sink.print("Creating cloudformation stack for container ...");
        let stack = self.aws.container_template(&ctx, c).await?;
        self.show_acm_manual_request(c, &stack, sink).await?;
        self.aws.create_stack(&stack).await?;
        self.wait_stack(&stack).await
    }

    /// 既にある container を更新する: image の差し替え → (template が変わっていれば)
    /// stack の更新 → DEPLOY_HASH の同期
    pub async fn update(
        &self,
        ctx: &Context,
        container: &str,
        sink: &mut dyn Sink,
        reload: impl FnOnce() -> Result<Context>,
    ) -> Result<()> {
        let SecretsReady(ctx) = self
            .mediator(ctx)
            .ensure_pocket_managed_secrets(sink, reload)
            .await?;
        let c = find(&ctx, container)?;
        let stack = self.aws.container_template(&ctx, c).await?;
        self.show_acm_manual_request(c, &stack, sink).await?;
        let mut image_uri = None;
        for (key, handler) in c.handlers.iter() {
            let name = &handler.function_name;
            if lambda::status(self.lambda, name).await? == ResourceStatus::NoExist {
                sink.print(&format!("function {key} was not found and skipped."));
                continue;
            }
            if image_uri.is_none() {
                let uri =
                    self.aws.repository_uri(c).await?.with_context(|| {
                        format!("ECR repository '{}' が存在しません。", c.ecr_name)
                    })?;
                image_uri = Some(format!("{uri}:{}", c.stage));
            }
            let uri = image_uri.as_deref().unwrap_or_default();
            lambda::update_code(self.lambda, sink, name, uri).await?;
        }
        for (_, handler) in c.handlers.iter() {
            lambda::wait_update(self.lambda, &handler.function_name, self.waits.lambda).await?;
        }
        let description = self.aws.describe_stack(&stack.region, &stack.name).await?;
        if !template_synced(description.as_ref(), &stack)? {
            self.aws.update_stack(&stack).await?;
            self.wait_stack(&stack).await?;
        }
        lambda::sync_deploy_hash(self.lambda, sink, c, self.waits.lambda).await
    }

    fn mediator<'m>(&'m self, ctx: &'m Context) -> Mediator<'m, S, D> {
        Mediator {
            ctx,
            store: self.store,
            urls: self.urls,
        }
    }

    async fn wait_stack(&self, stack: &StackTemplate) -> Result<()> {
        let stacks = RegionStacks {
            aws: self.aws,
            region: &stack.region,
        };
        wait_status(
            &stacks,
            &stack.name,
            "container",
            ResourceStatus::Completed,
            self.waits.stack,
        )
        .await
    }

    /// 証明書を手動で検証する構成 (独自ドメインで DNS record を pocket が作らない) で、
    /// 今回の stack 操作が証明書を作る・作り直すなら、手動対応が要ることを知らせる
    async fn show_acm_manual_request(
        &self,
        c: &ContainerContext,
        stack: &StackTemplate,
        sink: &mut dyn Sink,
    ) -> Result<()> {
        let refs = manual_cert_ref_names(c);
        if refs.is_empty() {
            return Ok(());
        }
        let uploaded = self
            .aws
            .uploaded_template(&stack.region, &stack.name)
            .await?;
        if acm_manual_update_required(&refs, uploaded.as_deref(), &stack.body)? {
            sink.warning("You need to request ACM manually to complete stack events.");
            sink.warning("See CloudFormation stack log.");
            sink.warning("Probably, you need to request dns A record to WsgiRegionalDomainName");
        }
        Ok(())
    }
}

fn find<'c>(ctx: &'c Context, container: &str) -> Result<&'c ContainerContext> {
    ctx.container
        .get(container)
        .with_context(|| format!("container '{container}' がこの stage に設定されていません。"))
}

/// DNS record を pocket が作らない独自ドメインの handler の、証明書リソースの名前
fn manual_cert_ref_names(c: &ContainerContext) -> Vec<&str> {
    c.handlers
        .values()
        .filter(|handler| {
            handler
                .apigateway
                .as_ref()
                .is_some_and(|api| api.domain.is_some() && !api.create_records)
        })
        .map(|handler| handler.cloudformation_cert_ref_name.as_str())
        .collect()
}

/// 今回の stack 操作で、手動検証の証明書が作られる・作り直されるか。
///
/// - deploy 済みの template が無い (これから作る)
/// - 証明書リソースが deploy 済みの template に無い
/// - 証明書リソースの定義が変わった
fn acm_manual_update_required(
    cert_ref_names: &[&str],
    uploaded: Option<&str>,
    template: &str,
) -> Result<bool> {
    if cert_ref_names.is_empty() {
        return Ok(false);
    }
    let Some(old) = uploaded.filter(|text| !text.trim().is_empty()) else {
        return Ok(true);
    };
    let old = parse_yaml(old).context("deploy 済みの template を読めません")?;
    let new = parse_yaml(template).context("template を読めません")?;
    if old.is_null() {
        return Ok(true);
    }
    Ok(cert_ref_names.iter().any(|name| {
        let before = &old["Resources"][*name];
        let after = &new["Resources"][*name];
        !after.is_null() && before != after
    }))
}

fn parse_yaml(text: &str) -> Result<Json> {
    Ok(serde_yaml_ng::from_str(text)?)
}

/// AWS に対する [`ContainerAws`]
pub struct AwsContainer<'a> {
    pub build: super::ecr::BuildEnv<'a>,
}

impl AwsContainer<'_> {
    fn cfn(&self, region: &str) -> crate::stack::ops::CfnClient {
        crate::stack::ops::CfnClient::new(self.build.sdk, region)
    }
}

impl ContainerAws for AwsContainer<'_> {
    async fn describe_stack(&self, region: &str, name: &str) -> Result<Option<StackDescription>> {
        self.cfn(region).describe(name).await
    }

    async fn stack_resources(&self, region: &str, name: &str) -> Result<Vec<StackResource>> {
        self.cfn(region).resources(name).await
    }

    async fn uploaded_template(&self, region: &str, name: &str) -> Result<Option<String>> {
        self.cfn(region).template(name).await
    }

    async fn create_stack(&self, stack: &StackTemplate) -> Result<()> {
        self.cfn(&stack.region).create(stack).await
    }

    async fn update_stack(&self, stack: &StackTemplate) -> Result<()> {
        self.cfn(&stack.region).update(stack).await
    }

    async fn add_consumer_tag(&self, region: &str, stack_arn: &str, slug: &str) -> Result<()> {
        if stack_arn.is_empty() {
            return Ok(());
        }
        let config = crate::aws_util::regional_config(self.build.sdk, region);
        aws_sdk_resourcegroupstagging::Client::new(&config)
            .tag_resources()
            .resource_arn_list(stack_arn)
            .tags(format!("pocket:consumer:{slug}"), "deployed")
            .send()
            .await
            .map_err(|e| crate::aws_util::sdk_error("tagging tag_resources", &e))?;
        Ok(())
    }

    async fn container_template(
        &self,
        ctx: &Context,
        c: &ContainerContext,
    ) -> Result<StackTemplate> {
        // 待機や secret の用意の後に呼ばれるので、毎回 AWS から引き直す
        crate::status::aws::AwsEvaluator::new(ctx, self.build.sdk.clone())
            .container_template(c)
            .await
    }

    async fn repository_uri(&self, c: &ContainerContext) -> Result<Option<String>> {
        let config = crate::aws_util::regional_config(self.build.sdk, &c.region);
        super::ecr::repository_uri(&aws_sdk_ecr::Client::new(&config), &c.ecr_name).await
    }

    async fn sync_image(&self, c: &ContainerContext) -> Result<()> {
        super::ecr::sync_image(&self.build, c).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    use std::cell::RefCell;
    use std::collections::{BTreeMap, BTreeSet};

    use magic_pocket_settings::context::SecretsContext;
    use magic_pocket_settings::model::StoreType;

    use super::super::lambda::{UpdateCodeError, UpdatedFunction};
    use super::super::secret_store::ManagedSecrets;
    use crate::stack::container as container_stack;
    use crate::stack::ops::TEMPLATE_HASH_TAG;
    use crate::stack::testing::context_from_toml;
    use crate::status::container::HandlerState;
    use crate::status::sink::{Stream, Style};

    /// 呼ばれた操作を 1 本の列に記録する fake (手順の順序を確かめる)
    #[derive(Default)]
    struct Fake {
        calls: RefCell<Vec<String>>,
        /// stack 名 → describe の結果
        stacks: RefCell<BTreeMap<String, StackDescription>>,
        uploaded: Option<String>,
        template_body: String,
        repository: Option<String>,
        /// 在る function の名前
        functions: BTreeSet<String>,
        env: RefCell<BTreeMap<String, BTreeMap<String, String>>>,
    }

    impl Fake {
        fn call(&self, text: String) {
            self.calls.borrow_mut().push(text);
        }

        fn with_stack(self, name: &str, status: &str, tags: &[(&str, &str)]) -> Self {
            self.stacks.borrow_mut().insert(
                name.to_string(),
                StackDescription {
                    stack_id: format!("arn:stack/{name}"),
                    status: status.to_string(),
                    tags: tags
                        .iter()
                        .map(|(k, v)| (k.to_string(), v.to_string()))
                        .collect(),
                    ..Default::default()
                },
            );
            self
        }

        fn template(&self, c: &ContainerContext) -> StackTemplate {
            StackTemplate {
                name: container_stack::stack_name(c),
                region: c.region.clone(),
                capabilities: Vec::new(),
                tags: Vec::new(),
                export: serde_json::Map::new(),
                body: self.template_body.clone(),
            }
        }
    }

    impl ContainerAws for Fake {
        async fn describe_stack(
            &self,
            _region: &str,
            name: &str,
        ) -> Result<Option<StackDescription>> {
            Ok(self.stacks.borrow().get(name).cloned())
        }

        async fn stack_resources(&self, _region: &str, _name: &str) -> Result<Vec<StackResource>> {
            Ok(Vec::new())
        }

        async fn uploaded_template(&self, _region: &str, _name: &str) -> Result<Option<String>> {
            Ok(self.uploaded.clone())
        }

        async fn create_stack(&self, stack: &StackTemplate) -> Result<()> {
            self.call(format!("create_stack {}", stack.name));
            self.stacks.borrow_mut().insert(
                stack.name.clone(),
                StackDescription {
                    status: "CREATE_COMPLETE".to_string(),
                    ..Default::default()
                },
            );
            Ok(())
        }

        async fn update_stack(&self, stack: &StackTemplate) -> Result<()> {
            self.call(format!("update_stack {}", stack.name));
            Ok(())
        }

        async fn add_consumer_tag(&self, _region: &str, stack_arn: &str, slug: &str) -> Result<()> {
            self.call(format!("add_consumer_tag {stack_arn} {slug}"));
            Ok(())
        }

        async fn container_template(
            &self,
            _ctx: &Context,
            c: &ContainerContext,
        ) -> Result<StackTemplate> {
            self.call("container_template".to_string());
            Ok(self.template(c))
        }

        async fn repository_uri(&self, _c: &ContainerContext) -> Result<Option<String>> {
            Ok(self.repository.clone())
        }

        async fn sync_image(&self, c: &ContainerContext) -> Result<()> {
            self.call(format!("sync_image {}", c.name));
            Ok(())
        }
    }

    impl LambdaApi for Fake {
        async fn state(&self, name: &str) -> Result<HandlerState> {
            Ok(HandlerState {
                last_update_status: self
                    .functions
                    .contains(name)
                    .then(|| "Successful".to_string()),
                code_sha256: None,
            })
        }

        async fn update_code(
            &self,
            name: &str,
            image_uri: &str,
        ) -> std::result::Result<UpdatedFunction, UpdateCodeError> {
            self.call(format!("update_code {name} {image_uri}"));
            Ok(UpdatedFunction::default())
        }

        async fn environment(&self, name: &str) -> Result<BTreeMap<String, String>> {
            Ok(self.env.borrow().get(name).cloned().unwrap_or_default())
        }

        async fn update_environment(
            &self,
            name: &str,
            env: &BTreeMap<String, String>,
        ) -> Result<()> {
            self.call(format!(
                "update_environment {name} DEPLOY_HASH={}",
                env.get("DEPLOY_HASH").map(String::as_str).unwrap_or("")
            ));
            self.env.borrow_mut().insert(name.to_string(), env.clone());
            Ok(())
        }
    }

    impl SecretStore for Fake {
        async fn read(&self, _view: &SecretsContext) -> Result<ManagedSecrets> {
            Ok(ManagedSecrets::new())
        }

        async fn update(&self, view: &SecretsContext, _secrets: &ManagedSecrets) -> Result<()> {
            self.call(format!("update_secrets {}", view.pocket_key));
            Ok(())
        }

        async fn delete_keys(
            &self,
            _view: &SecretsContext,
            _keys: &BTreeSet<String>,
        ) -> Result<()> {
            Ok(())
        }

        async fn user_secret_exists(
            &self,
            _name: &str,
            _store: StoreType,
            _region: &str,
        ) -> Result<bool> {
            Ok(true)
        }
    }

    impl DatabaseUrls for Fake {
        async fn neon_database_url(&self, _ctx: &Context) -> Result<Option<String>> {
            Ok(None)
        }
        async fn tidb_database_url(&self, _ctx: &Context) -> Result<Option<String>> {
            Ok(None)
        }
        async fn upstash_redis_url(&self, _ctx: &Context) -> Result<Option<String>> {
            Ok(None)
        }
    }

    #[derive(Default)]
    struct Recorder(Vec<(Style, String)>);

    impl Sink for Recorder {
        fn emit(&mut self, _stream: Stream, style: Style, text: &str) {
            self.0.push((style, text.to_string()));
        }
    }

    impl Recorder {
        fn texts(&self, style: Style) -> Vec<&str> {
            self.0
                .iter()
                .filter(|(s, _)| *s == style)
                .map(|(_, text)| text.as_str())
                .collect()
        }
    }

    fn deploy(fake: &Fake) -> ContainerDeploy<'_, Fake, Fake, Fake, Fake> {
        let quick = WaitOptions {
            attempts: 3,
            interval: std::time::Duration::ZERO,
            timeout_secs: 1,
        };
        ContainerDeploy {
            aws: fake,
            lambda: fake,
            store: fake,
            urls: fake,
            waits: Waits {
                vpc: quick,
                stack: quick,
                lambda: WaitUpdate {
                    interval: std::time::Duration::from_millis(1),
                    attempts: 3,
                },
            },
        }
    }

    fn toml(extra: &str) -> String {
        format!(
            r#"
[general]
region = "ap-southeast-1"
project_name = "testprj"
stages = ["prod"]
{extra}
[container.main]
dockerfile_path = "Dockerfile"
envs = {{ DEPLOY_HASH = "new" }}

[container.main.secrets.managed]
SECRET_KEY = {{ type = "password" }}

[container.main.handlers.wsgi]
command = "a.b"

[container.main.handlers.worker]
command = "a.c"
"#
        )
    }

    const EXTERNAL_VPC: &str = "[vpc]\nref = \"shared\"\nmanage = false\n";
    const MANAGED_VPC: &str = "[vpc]\nref = \"own\"\nzone_suffixes = [\"a\"]\n";
    const STACK: &str = "prod-testprj-container-main";
    const SECRETS: &str = "prod-testprj-main-pocket";

    fn main(ctx: &Context) -> &ContainerContext {
        ctx.container.get("main").expect("container")
    }

    #[tokio::test]
    async fn test_deploy_init_without_vpc_only_syncs_image() {
        let ctx = context_from_toml(&toml(""), "prod");
        let fake = Fake::default();
        deploy(&fake).deploy_init(main(&ctx)).await.unwrap();
        assert_eq!(*fake.calls.borrow(), vec!["sync_image main"]);
    }

    #[tokio::test]
    async fn test_deploy_init_checks_external_vpc_and_tags_consumer() {
        let ctx = context_from_toml(&toml(EXTERNAL_VPC), "prod");
        let c = main(&ctx);
        let vpc = vpc_stack::stack_name(c.vpc.as_ref().expect("vpc"));

        let fake = Fake::default();
        let error = deploy(&fake).deploy_init(c).await.unwrap_err();
        assert_eq!(
            error.to_string(),
            format!("外部 VPC スタック '{vpc}' が見つかりません。")
        );

        let fake = Fake::default().with_stack(&vpc, "CREATE_COMPLETE", &[]);
        let error = deploy(&fake).deploy_init(c).await.unwrap_err();
        assert_eq!(
            error.to_string(),
            format!("VPC '{vpc}' は共有が許可されていません (sharable = true が必要です)")
        );

        let fake =
            Fake::default().with_stack(&vpc, "CREATE_COMPLETE", &[("pocket:sharable", "true")]);
        deploy(&fake).deploy_init(c).await.unwrap();
        assert_eq!(
            *fake.calls.borrow(),
            vec![
                "sync_image main".to_string(),
                format!("add_consumer_tag arn:stack/{vpc} {}", c.slug),
            ]
        );
    }

    #[tokio::test]
    async fn test_create_orders_secrets_before_stack() {
        let ctx = context_from_toml(&toml(""), "prod");
        assert_eq!(container_stack::stack_name(main(&ctx)), STACK);
        let fake = Fake::default();
        let mut sink = Recorder::default();
        let reloaded = RefCell::new(false);
        deploy(&fake)
            .create(&ctx, "main", &mut sink, || {
                *reloaded.borrow_mut() = true;
                Ok(ctx.clone())
            })
            .await
            .unwrap();
        assert_eq!(
            *fake.calls.borrow(),
            vec![
                format!("update_secrets {SECRETS}"),
                "container_template".to_string(),
                format!("create_stack {STACK}"),
            ]
        );
        // template は secret の用意の後に作り直した Context から描く
        assert!(*reloaded.borrow());
        assert_eq!(
            sink.texts(Style::Plain),
            vec![
                "Creating secrets ...",
                "Creating cloudformation stack for container ..."
            ]
        );
    }

    #[tokio::test]
    async fn test_create_waits_for_managed_vpc() {
        let ctx = context_from_toml(&toml(MANAGED_VPC), "prod");
        let vpc = vpc_stack::stack_name(main(&ctx).vpc.as_ref().expect("vpc"));
        // VPC の stack が完成しないうちは secret にも stack にも手を付けない
        let fake = Fake::default().with_stack(&vpc, "CREATE_IN_PROGRESS", &[]);
        let result = deploy(&fake)
            .create(&ctx, "main", &mut Recorder::default(), || Ok(ctx.clone()))
            .await;
        assert!(result.is_err());
        assert!(fake.calls.borrow().is_empty(), "{:?}", fake.calls);

        let fake = Fake::default().with_stack(&vpc, "CREATE_COMPLETE", &[]);
        deploy(&fake)
            .create(&ctx, "main", &mut Recorder::default(), || Ok(ctx.clone()))
            .await
            .unwrap();
        assert!(fake
            .calls
            .borrow()
            .contains(&format!("create_stack {STACK}")));
    }

    #[tokio::test]
    async fn test_update_skips_missing_functions_and_updates_stale_stack() {
        let ctx = context_from_toml(&toml(""), "prod");
        let c = main(&ctx);
        let wsgi = c.handlers.get("wsgi").unwrap().function_name.clone();
        let fake = Fake {
            repository: Some("123.dkr.ecr/repo".to_string()),
            functions: BTreeSet::from([wsgi.clone()]),
            template_body: "Resources: {}\n".to_string(),
            ..Default::default()
        }
        .with_stack(STACK, "UPDATE_COMPLETE", &[(TEMPLATE_HASH_TAG, "stale")]);
        let mut sink = Recorder::default();
        deploy(&fake)
            .update(&ctx, "main", &mut sink, || Ok(ctx.clone()))
            .await
            .unwrap();
        assert_eq!(
            *fake.calls.borrow(),
            vec![
                format!("update_secrets {SECRETS}"),
                "container_template".to_string(),
                format!("update_code {wsgi} 123.dkr.ecr/repo:prod"),
                format!("update_stack {STACK}"),
                format!("update_environment {wsgi} DEPLOY_HASH=new"),
            ]
        );
        assert!(sink
            .texts(Style::Plain)
            .contains(&"function worker was not found and skipped."));
    }

    #[tokio::test]
    async fn test_update_leaves_synced_stack_alone() {
        let ctx = context_from_toml(&toml(""), "prod");
        let c = main(&ctx);
        let fake = Fake {
            template_body: "Resources: {}\n".to_string(),
            ..Default::default()
        };
        let hash = fake.template(c).template_hash();
        let fake = fake.with_stack(STACK, "UPDATE_COMPLETE", &[(TEMPLATE_HASH_TAG, &hash)]);
        deploy(&fake)
            .update(&ctx, "main", &mut Recorder::default(), || Ok(ctx.clone()))
            .await
            .unwrap();
        assert_eq!(
            *fake.calls.borrow(),
            vec![
                format!("update_secrets {SECRETS}"),
                "container_template".to_string()
            ]
        );
    }

    #[tokio::test]
    async fn test_acm_warning_is_shown_before_stack_operation() {
        let extra = "";
        let text = toml(extra).replace(
            "[container.main.handlers.worker]",
            "[container.main.handlers.wsgi.apigateway]\ndomain = \"app.example.com\"\ncreate_records = false\n\n[container.main.handlers.worker]",
        );
        let ctx = context_from_toml(&text, "prod");
        let fake = Fake {
            template_body: "Resources: {}\n".to_string(),
            ..Default::default()
        };
        let mut sink = Recorder::default();
        deploy(&fake)
            .create(&ctx, "main", &mut sink, || Ok(ctx.clone()))
            .await
            .unwrap();
        assert_eq!(
            sink.texts(Style::Warning),
            vec![
                "You need to request ACM manually to complete stack events.",
                "See CloudFormation stack log.",
                "Probably, you need to request dns A record to WsgiRegionalDomainName",
            ]
        );
    }

    const OLD: &str = "Resources:\n  WsgiCertificate:\n    Type: AWS::CertificateManager::Certificate\n    Properties:\n      DomainName: a.example.com\n  Other:\n    Type: X\n";

    #[test]
    fn test_acm_not_required_without_manual_certificates() {
        assert!(!acm_manual_update_required(&[], None, OLD).unwrap());
    }

    #[test]
    fn test_acm_required_when_stack_is_new() {
        assert!(acm_manual_update_required(&["WsgiCertificate"], None, OLD).unwrap());
        assert!(acm_manual_update_required(&["WsgiCertificate"], Some(""), OLD).unwrap());
    }

    #[test]
    fn test_acm_required_when_certificate_is_added_or_changed() {
        let without = "Resources:\n  Other:\n    Type: X\n";
        assert!(acm_manual_update_required(&["WsgiCertificate"], Some(without), OLD).unwrap());
        let changed = OLD.replace("a.example.com", "b.example.com");
        assert!(acm_manual_update_required(&["WsgiCertificate"], Some(OLD), &changed).unwrap());
    }

    #[test]
    fn test_acm_not_required_when_only_other_resources_change() {
        let changed = OLD.replace("Type: X", "Type: Y");
        assert!(!acm_manual_update_required(&["WsgiCertificate"], Some(OLD), &changed).unwrap());
        // key の順や書式だけの違いは変更と見なさない
        let reordered = "Resources:\n  Other: {Type: X}\n  WsgiCertificate:\n    Properties: {DomainName: a.example.com}\n    Type: AWS::CertificateManager::Certificate\n";
        assert!(!acm_manual_update_required(&["WsgiCertificate"], Some(OLD), reordered).unwrap());
    }
}
