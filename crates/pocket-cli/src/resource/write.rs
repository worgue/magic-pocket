//! `pocket-rs resource` の書き込み (Python 版の create / update / destroy、container の
//! secret・env 操作、cloudfront の upload / prune)。
//!
//! 操作の本体は `deploy/` の部品 (`pocket-rs deploy` / `destroy` と同じ実装) を使い、
//! ここは対象の選択・状態による分岐・確認・文言だけを持つ。確認の有無と `-y` の扱いは
//! Python 版と同じ (vpc / s3 / rds / dsql の destroy と delete-pocket-managed は `-y` を
//! 持たず、常に聞く)

use std::collections::BTreeMap;

use anyhow::{bail, Result};
use magic_pocket_settings::context::{CloudFrontContext, Context, RouteContext};

use super::container::{fresh_secrets, lambda_env, target_handlers};
use super::{resolve_container, ContainerTarget, EdgeTarget, VpcTarget};
use crate::aws_util::{regional_config, sdk_error};
use crate::context::load_context;
use crate::deploy::dsql::AwsDsql;
use crate::deploy::edge::{EdgeDeploy, EdgeWaits};
use crate::deploy::inbound::AwsInbound;
use crate::deploy::inbound_domain::AwsInboundDomain;
use crate::deploy::interaction::{Interaction, Prompt};
use crate::deploy::lambda::{AwsLambda, LambdaApi};
use crate::deploy::mediator::{ErrorLevel, Mediator};
use crate::deploy::parts::{
    cloudfront_deploy, cloudfront_env, dsql_deploy, inbound_deploy, inbound_domain_deploy,
    rds_deploy, rds_env, vpc_deploy, vpc_env, Env,
};
use crate::deploy::s3::{delete_bucket_with_contents, BucketWriter, CreateRetry};
use crate::deploy::secret_store::{read_managed, AwsSecretStore};
use crate::deploy::stacks::{AwsStacks, Stacks as _};
use crate::deploy::stored_value::AwsStoredValues;
use crate::deploy::upload::{local_files, s3_prefix, Uploader};
use crate::stack::ops::{cfn_status, ResourceStatus, StackApi};
use crate::stack::vpc::stack_name as vpc_stack_name;
use crate::status::aws::AwsEvaluator;
use crate::status::report::Evaluator;
use crate::status::resource::Resource;
use crate::status::sink::Sink;

/// 確認 (`-y` なら聞かない)。断られたら Python の `click.confirm(abort=True)` と同じく中止
fn confirm(text: &str, yes: bool) -> Result<()> {
    if !(Interaction { assume_yes: yes }).confirm(text, false)? {
        bail!("Aborted!");
    }
    Ok(())
}

// ---- vpc ----

/// VPC の状態 (stack の状態と template の同期)
async fn vpc_status(
    sdk: &aws_config::SdkConfig,
    vpc: &magic_pocket_settings::context::VpcContext,
) -> Result<ResourceStatus> {
    let description = crate::stack::ops::CfnClient::new(sdk, &vpc.region)
        .describe(&vpc_stack_name(vpc))
        .await?;
    let status = cfn_status(description.as_ref())?;
    if status != ResourceStatus::Completed {
        return Ok(status);
    }
    let template = crate::status::aws::vpc_template(sdk, vpc).await?;
    crate::stack::ops::stack_status(description.as_ref(), &template)
}

pub async fn vpc_create(target: &VpcTarget, sink: &mut dyn Sink) -> Result<()> {
    let vpc = super::vpc::vpc_context(target)?;
    if !vpc.manage {
        sink.danger("外部 VPC は他で管理されています。");
        return Ok(());
    }
    let sdk = crate::aws_util::load_sdk().await;
    if vpc_status(&sdk, &vpc).await? != ResourceStatus::NoExist {
        sink.warning("AWS vpc is already created.");
        return Ok(());
    }
    let (stacks, efs, network) = vpc_env(&sdk);
    vpc_deploy(&stacks, &efs, &network).create(&vpc).await?;
    sink.success("Created: vpc");
    Ok(())
}

pub async fn vpc_update(target: &VpcTarget, sink: &mut dyn Sink) -> Result<()> {
    let vpc = super::vpc::vpc_context(target)?;
    if !vpc.manage {
        sink.danger("外部 VPC は他で管理されています。");
        return Ok(());
    }
    let sdk = crate::aws_util::load_sdk().await;
    match vpc_status(&sdk, &vpc).await? {
        ResourceStatus::NoExist => sink.warning("vpc has not created yet."),
        ResourceStatus::Failed => sink.danger("vpc has failed. Please check console."),
        ResourceStatus::Progress => sink.warning("vpc is updating. Please wait."),
        _ => {
            let (stacks, efs, network) = vpc_env(&sdk);
            vpc_deploy(&stacks, &efs, &network).update(&vpc).await?;
        }
    }
    Ok(())
}

pub async fn vpc_destroy(target: &VpcTarget, sink: &mut dyn Sink) -> Result<()> {
    let vpc = super::vpc::vpc_context(target)?;
    if !vpc.manage {
        sink.danger("外部 VPC は他で管理されています。");
        return Ok(());
    }
    let sdk = crate::aws_util::load_sdk().await;
    let description = crate::stack::ops::CfnClient::new(&sdk, &vpc.region)
        .describe(&vpc_stack_name(&vpc))
        .await?;
    let consumers: Vec<String> = description
        .as_ref()
        .map(|d| d.tags.as_slice())
        .unwrap_or_default()
        .iter()
        .filter_map(|(key, _)| key.strip_prefix("pocket:consumer:"))
        .map(str::to_string)
        .collect();
    if !consumers.is_empty() {
        sink.danger("VPC に consumer がいるため削除できません:");
        for consumer in &consumers {
            sink.info(&format!("  - {consumer}"));
        }
        return Ok(());
    }
    let has_stack = cfn_status(description.as_ref())? != ResourceStatus::NoExist;
    let (stacks, efs, network) = vpc_env(&sdk);
    let has_efs = match vpc.efs.as_ref() {
        Some(context) => {
            crate::deploy::efs::status(&efs, context).await? != ResourceStatus::NoExist
        }
        None => false,
    };
    if !has_stack && !has_efs {
        sink.warning("No VPC resources found.");
        return Ok(());
    }
    confirm("VPC を削除しますか？", false)?;
    vpc_deploy(&stacks, &efs, &network).delete(&vpc).await?;
    sink.success("VPC was destroyed.");
    Ok(())
}

// ---- container ----

fn container<'c>(
    context: &'c Context,
    target: &ContainerTarget,
) -> Result<&'c magic_pocket_settings::context::ContainerContext> {
    if context.container.is_empty() {
        bail!("container is not configured for this stage");
    }
    resolve_container(context, target.container.as_deref())
}

async fn container_status(
    context: &Context,
    sdk: &aws_config::SdkConfig,
    c: &magic_pocket_settings::context::ContainerContext,
    sink: &mut dyn Sink,
) -> Result<ResourceStatus> {
    AwsEvaluator::new(context, sdk.clone())
        .status(&Resource::Container(c), sink)
        .await
}

pub async fn container_create(target: &ContainerTarget, sink: &mut dyn Sink) -> Result<()> {
    let stage = &target.stage.stage;
    let sdk = crate::aws_util::load_sdk().await;
    let (context, toml_dir) = load_context(stage)?;
    let c = container(&context, target)?;
    if container_status(&context, &sdk, c, sink).await? != ResourceStatus::NoExist {
        sink.warning("AWS lambda container is already created.");
        return Ok(());
    }
    let env = Env::new(&sdk, &context, &toml_dir).await?;
    let aws = env.aws();
    env.deploy(&aws)
        .create(&context, &c.name, sink, || Ok(load_context(stage)?.0))
        .await?;
    sink.success("Created: lambda");
    Ok(())
}

pub async fn container_update(target: &ContainerTarget, sink: &mut dyn Sink) -> Result<()> {
    let stage = &target.stage.stage;
    let sdk = crate::aws_util::load_sdk().await;
    let (context, toml_dir) = load_context(stage)?;
    let c = container(&context, target)?;
    match container_status(&context, &sdk, c, sink).await? {
        ResourceStatus::NoExist => sink.warning("AWS lambda has not created yet."),
        ResourceStatus::Failed => sink.danger("AWS lambda has failed. Please check console."),
        ResourceStatus::Progress => sink.warning("AWS lambda is updating. Please wait."),
        _ => {
            let env = Env::new(&sdk, &context, &toml_dir).await?;
            let aws = env.aws();
            env.deploy(&aws)
                .update(&context, &c.name, sink, || Ok(load_context(stage)?.0))
                .await?;
        }
    }
    Ok(())
}

/// 反映する環境変数 (今の値に secret の最新値を重ねる) と、値が変わる key
fn reloaded_env(
    current: &BTreeMap<String, String>,
    fresh: &BTreeMap<String, String>,
) -> (BTreeMap<String, String>, Vec<String>) {
    let mut env = current.clone();
    env.extend(fresh.iter().map(|(k, v)| (k.clone(), v.clone())));
    let changed = super::container::drift_keys(current, fresh);
    (env, changed)
}

pub async fn reload_env(
    target: &ContainerTarget,
    handler: Option<&str>,
    sink: &mut dyn Sink,
) -> Result<()> {
    let stage = &target.stage.stage;
    let (context, _) = load_context(stage)?;
    if context.container.is_empty() {
        bail!("[container.<name>] が設定されていません");
    }
    let c = resolve_container(&context, target.container.as_deref())?;
    let fresh = fresh_secrets(stage, &c.name).await?;
    if fresh.is_empty() {
        sink.warning("secrets が宣言されていません。何もしません。");
        return Ok(());
    }
    let sdk = crate::aws_util::load_sdk().await;
    let client = aws_sdk_lambda::Client::new(&regional_config(&sdk, &c.region));
    let lambda = AwsLambda::new(&sdk, &c.region);
    for name in target_handlers(c, handler)? {
        let function_name = &c.handlers.get(&name).expect("handler").function_name;
        let current = lambda_env(&client, function_name).await?;
        let (env, changed) = reloaded_env(&current, &fresh);
        if env == current {
            sink.info(&format!("[{name}] 差分なし (handler 内 env は既に最新)"));
            continue;
        }
        lambda.update_environment(function_name, &env).await?;
        sink.success(&format!(
            "[{name}] env を更新しました ({}/{} 秘密値を反映、warm container は再生成)",
            changed.len(),
            fresh.len()
        ));
        for key in &changed {
            sink.log(&format!("  - {key}"));
        }
    }
    Ok(())
}

/// shared store + 名前順の各 container store (Python の `_secrets_views`)
fn secrets_views(
    context: &Context,
) -> Vec<(&str, &magic_pocket_settings::context::SecretsContext)> {
    let mut views = Vec::new();
    if let Some(shared) = context.secrets.as_ref() {
        views.push(("shared", shared));
    }
    let mut names: Vec<&String> = context.container.keys().collect();
    names.sort();
    for name in names {
        if let Some(sc) = context.container.get(name).and_then(|c| c.secrets.as_ref()) {
            views.push((name.as_str(), sc));
        }
    }
    views
}

pub async fn create_pocket_managed(stage: &str, sink: &mut dyn Sink) -> Result<()> {
    let (context, _) = load_context(stage)?;
    if secrets_views(&context).is_empty() {
        sink.warning("secrets is not configured for this stage");
        return Ok(());
    }
    let sdk = crate::aws_util::load_sdk().await;
    let store = AwsSecretStore::new(sdk);
    Mediator {
        ctx: &context,
        store: &store,
    }
    .create_pocket_managed_secrets(sink, ErrorLevel::Warning)
    .await
}

pub async fn delete_pocket_managed(stage: &str, sink: &mut dyn Sink) -> Result<()> {
    let (context, _) = load_context(stage)?;
    let views = secrets_views(&context);
    if views.is_empty() {
        sink.warning("secrets is not configured");
        return Ok(());
    }
    let sdk = crate::aws_util::load_sdk().await;
    let mut existing = Vec::new();
    for (label, sc) in &views {
        if sc.managed.is_empty() {
            continue;
        }
        let stored = read_managed(&sdk, sc, true).await?.unwrap_or_default();
        for key in sc.managed.keys().filter(|key| stored.contains_key(*key)) {
            existing.push(format!("{key} ({label})"));
        }
    }
    // Python 版と同じく、作られた secret が無ければ確認せずに削除へ進む (消すものは無い)
    if existing.is_empty() {
        sink.warning("No pocket managed secets are created yet.");
    } else {
        sink.warning("You are deleting pocket managed secrets.");
        sink.info("Deleting secrets:");
        for key in &existing {
            sink.info(&format!(" - {key}"));
        }
        sink.danger("This data cannot be restored!");
        confirm("Do you realy want to delete pocket managed secrets?", false)?;
    }
    let store = AwsSecretStore::new(sdk);
    for (_, sc) in &views {
        store.delete_all(sc).await?;
    }
    Ok(())
}

// ---- s3 ----

pub async fn s3_create(stage: &str, sink: &mut dyn Sink) -> Result<()> {
    let (context, _) = load_context(stage)?;
    let Some(s3) = context.s3.as_ref() else {
        bail!("s3 is not configured for this stage");
    };
    let sdk = crate::aws_util::load_sdk().await;
    let client = aws_sdk_s3::Client::new(&regional_config(&sdk, &s3.region));
    let writer = BucketWriter {
        client: &client,
        s3,
        cloudfront: &context.cloudfront,
        retry: CreateRetry::default(),
    };
    if writer.exists().await? {
        writer.update(sink).await?;
        sink.success(&format!(
            "Reconciled: bucket {} (PAB / CORS / versioning / lifecycle)",
            s3.bucket_name
        ));
    } else {
        writer.create(sink).await?;
        sink.success(&format!("Created: bucket {}", s3.bucket_name));
    }
    Ok(())
}

pub async fn s3_destroy(stage: &str, sink: &mut dyn Sink) -> Result<()> {
    let (context, _) = load_context(stage)?;
    let Some(s3) = context.s3.as_ref() else {
        bail!("s3 is not configured for this stage");
    };
    let sdk = crate::aws_util::load_sdk().await;
    let client = aws_sdk_s3::Client::new(&regional_config(&sdk, &s3.region));
    if !crate::deploy::s3::bucket_exists(&client, &s3.bucket_name).await? {
        sink.warning("No S3 bucket found.");
        return Ok(());
    }
    sink.danger("S3バケットの全データが失われます。");
    confirm(
        &format!("バケット '{}' を削除しますか？", s3.bucket_name),
        false,
    )?;
    delete_bucket_with_contents(&client, &s3.bucket_name).await?;
    sink.success("S3 bucket was deleted.");
    Ok(())
}

// ---- rds / dsql ----

pub async fn rds_destroy(stage: &str, sink: &mut dyn Sink) -> Result<()> {
    let (context, _) = load_context(stage)?;
    let Some(rds) = context.rds.as_ref() else {
        bail!("rds is not configured for this stage");
    };
    let sdk = crate::aws_util::load_sdk().await;
    let status = AwsEvaluator::new(&context, sdk.clone())
        .status(&Resource::Rds(rds), sink)
        .await?;
    if status == ResourceStatus::NoExist {
        sink.info("RDS cluster does not exist.");
        return Ok(());
    }
    confirm(
        &format!(
            "RDS Aurora クラスター '{}' を削除しますか？",
            rds.cluster_identifier
        ),
        false,
    )?;
    let (aws, stacks, values) = rds_env(&sdk, &rds.region);
    rds_deploy(&aws, &stacks, &values).delete(rds, sink).await?;
    sink.success("RDS Aurora cluster was destroyed. Final snapshot was created.");
    Ok(())
}

pub async fn dsql_destroy(stage: &str, sink: &mut dyn Sink) -> Result<()> {
    let (context, _) = load_context(stage)?;
    let Some(dsql) = context.dsql.as_ref() else {
        bail!("dsql is not configured for this stage");
    };
    let sdk = crate::aws_util::load_sdk().await;
    let status = AwsEvaluator::new(&context, sdk.clone())
        .status(&Resource::Dsql(dsql), sink)
        .await?;
    if status == ResourceStatus::NoExist {
        sink.info("DSQL cluster does not exist.");
        return Ok(());
    }
    confirm(
        &format!("DSQL クラスター '{}' を削除しますか？", dsql.tag_name),
        false,
    )?;
    let (api, values) = (AwsDsql { sdk: &sdk }, AwsStoredValues { sdk: &sdk });
    dsql_deploy(&api, &values).delete(dsql, sink).await
}

// ---- cloudfront ----

/// 対象の cloudfront (Python の `get_cloudfront_resources`)
fn cloudfronts<'c>(context: &'c Context, name: Option<&str>) -> Result<Vec<&'c CloudFrontContext>> {
    if context.cloudfront.is_empty() {
        bail!("cloudfront is not configured for this stage");
    }
    match name.filter(|n| !n.is_empty()) {
        Some(name) => match context.cloudfront.get(name) {
            Some(cf) => Ok(vec![cf]),
            None => bail!("cloudfront '{name}' is not configured"),
        },
        None => Ok(context.cloudfront.values().collect()),
    }
}

pub async fn cloudfront_create(target: &EdgeTarget, sink: &mut dyn Sink) -> Result<()> {
    let (context, _) = load_context(&target.stage.stage)?;
    let sdk = crate::aws_util::load_sdk().await;
    let (stacks, aws, store) = cloudfront_env(&sdk);
    let deploy = cloudfront_deploy(&stacks, &aws, &store);
    for cf in cloudfronts(&context, target.name.as_deref())? {
        sink.info(&format!("[{}]", cf.name));
        deploy.update(&context, &cf.name, sink).await?;
    }
    sink.success("cloudfront store was created");
    Ok(())
}

pub async fn cloudfront_update(target: &EdgeTarget, sink: &mut dyn Sink) -> Result<()> {
    let (context, _) = load_context(&target.stage.stage)?;
    let sdk = crate::aws_util::load_sdk().await;
    let (stacks, aws, store) = cloudfront_env(&sdk);
    let deploy = cloudfront_deploy(&stacks, &aws, &store);
    for cf in cloudfronts(&context, target.name.as_deref())? {
        sink.info(&format!("[{}]", cf.name));
        let name = crate::stack::cloudfront::cloudfront_stack_name(cf);
        let description = crate::stack::ops::CfnClient::new(&sdk, &cf.region)
            .describe(&name)
            .await?;
        match cfn_status(description.as_ref())? {
            ResourceStatus::NoExist => sink.warning("CloudFront resource has not created yet."),
            ResourceStatus::Failed => {
                sink.danger("CloudFront resource creation has failed. Please check console.")
            }
            ResourceStatus::Progress => sink.warning("CloudFront is updating. Please wait."),
            _ => deploy.update(&context, &cf.name, sink).await?,
        }
    }
    Ok(())
}

pub async fn cloudfront_destroy(target: &EdgeTarget, yes: bool, sink: &mut dyn Sink) -> Result<()> {
    let stage = &target.stage.stage;
    // Python 版と同じく、対象を確かめる前に聞く
    confirm(
        &format!("stage '{stage}' の CloudFront distribution を削除しますか？"),
        yes,
    )?;
    let (context, _) = load_context(stage)?;
    let sdk = crate::aws_util::load_sdk().await;
    let (stacks, aws, store) = cloudfront_env(&sdk);
    let deploy = cloudfront_deploy(&stacks, &aws, &store);
    for cf in cloudfronts(&context, target.name.as_deref())? {
        sink.info(&format!("[{}]", cf.name));
        deploy.delete(cf, sink).await?;
    }
    sink.success("cloudfront store was deleted successfully.");
    Ok(())
}

pub async fn cloudfront_upload(
    target: &EdgeTarget,
    skip_build: bool,
    sink: &mut dyn Sink,
) -> Result<()> {
    let (context, toml_dir) = load_context(&target.stage.stage)?;
    let sdk = crate::aws_util::load_sdk().await;
    let (stacks, aws, store) = cloudfront_env(&sdk);
    let deploy = cloudfront_deploy(&stacks, &aws, &store);
    for cf in cloudfronts(&context, target.name.as_deref())? {
        if cf.uploadable_routes.is_empty() {
            sink.info(&format!(
                "[{}] アップロード対象のルートがありません",
                cf.name
            ));
            continue;
        }
        sink.info(&format!("[{}] アップロード開始", cf.name));
        let distribution_id = deploy.distribution_id(cf).await?;
        let client = aws_sdk_s3::Client::new(&regional_config(&sdk, &cf.s3_region));
        let uploader = Uploader {
            sdk: &sdk,
            client: &client,
            cf,
            project_dir: &toml_dir,
        };
        let routes: Vec<&RouteContext> = cf.uploadable_routes.iter().collect();
        uploader
            .upload(&routes, skip_build, &distribution_id, sink)
            .await?;
    }
    sink.success("アップロード完了");
    Ok(())
}

/// route の S3 prefix 配下で、ローカルの upload_dir に無い key (旧版)。昇順
fn stale_keys(existing: &[String], local: &BTreeMap<String, std::path::PathBuf>) -> Vec<String> {
    let mut keys: Vec<String> = existing
        .iter()
        .filter(|key| !local.contains_key(*key))
        .cloned()
        .collect();
    keys.sort();
    keys
}

async fn list_keys(client: &aws_sdk_s3::Client, bucket: &str, prefix: &str) -> Result<Vec<String>> {
    let mut keys = Vec::new();
    let mut pages = client
        .list_objects_v2()
        .bucket(bucket)
        .prefix(prefix)
        .into_paginator()
        .send();
    while let Some(page) = pages.next().await {
        let page = page.map_err(|e| sdk_error("s3 list_objects_v2", &e))?;
        keys.extend(
            page.contents()
                .iter()
                .filter_map(|o| o.key().map(str::to_string)),
        );
    }
    Ok(keys)
}

pub async fn cloudfront_prune(
    target: &EdgeTarget,
    path_pattern: Option<&str>,
    dry_run: bool,
    yes: bool,
    sink: &mut dyn Sink,
) -> Result<()> {
    let (context, toml_dir) = load_context(&target.stage.stage)?;
    let mut targets: Vec<(&CloudFrontContext, aws_sdk_s3::Client, Vec<String>)> = Vec::new();
    let mut matched = false;
    for cf in cloudfronts(&context, target.name.as_deref())? {
        let client = crate::s3_client(&cf.s3_region).await;
        for route in &cf.uploadable_routes {
            if path_pattern.is_some_and(|p| p != route.path_pattern) {
                continue;
            }
            matched = true;
            let prefix = s3_prefix(route);
            let upload_dir = route.upload_dir.as_deref().unwrap_or_default();
            let local = local_files(&toml_dir.join(upload_dir), &prefix)?;
            let existing = list_keys(&client, &cf.bucket_name, &format!("{prefix}/")).await?;
            let keys = stale_keys(&existing, &local);
            let pattern = if route.path_pattern.is_empty() {
                "/*"
            } else {
                route.path_pattern.as_str()
            };
            sink.info(&format!("[{}] {pattern}: 旧版 {} 件", cf.name, keys.len()));
            for key in &keys {
                sink.log(&format!("  s3://{}/{key}", cf.bucket_name));
            }
            if !keys.is_empty() {
                targets.push((cf, client.clone(), keys));
            }
        }
    }
    if let (Some(pattern), false) = (path_pattern, matched) {
        bail!(
            "route '{pattern}' はアップロード対象 (build / upload_dir を持つ route) にありません"
        );
    }
    let total: usize = targets.iter().map(|(_, _, keys)| keys.len()).sum();
    if total == 0 {
        sink.success("削除対象の旧版はありません");
        return Ok(());
    }
    if dry_run {
        sink.info(&format!("--dry-run のため削除しません ({total} 件)"));
        return Ok(());
    }
    confirm(&format!("上記 {total} 件を削除しますか？"), yes)?;
    for (cf, client, keys) in &targets {
        for key in keys {
            client
                .delete_object()
                .bucket(&cf.bucket_name)
                .key(key)
                .send()
                .await
                .map_err(|e| sdk_error("s3 delete_object", &e))?;
            sink.log(&format!("削除: s3://{}/{key}", cf.bucket_name));
        }
    }
    sink.success(&format!("{total} 件を削除しました"));
    Ok(())
}

// ---- cloudfront-keys / cloudfront-waf ----

pub async fn edge_destroy(
    keys: bool,
    target: &EdgeTarget,
    yes: bool,
    sink: &mut dyn Sink,
) -> Result<()> {
    let stage = &target.stage.stage;
    let question = if keys {
        format!(
            "stage '{stage}' の CloudFront signing key リソースを削除しますか？\
             (署名済み URL が無効になります)"
        )
    } else {
        format!("stage '{stage}' の WAF WebACL を削除しますか？(IP 制限が解除されます)")
    };
    confirm(&question, yes)?;
    let (context, _) = load_context(stage)?;
    if context.cloudfront.is_empty() {
        bail!("cloudfront is not configured for this stage");
    }
    let name = target.name.as_deref().filter(|n| !n.is_empty());
    let selected: Vec<&CloudFrontContext> = context
        .cloudfront
        .iter()
        .filter(|(_, cf)| {
            if keys {
                cf.signing_key.as_deref().is_some_and(|k| !k.is_empty())
            } else {
                cf.waf.is_some()
            }
        })
        .filter(|(n, _)| name.is_none_or(|name| name == n.as_str()))
        .map(|(_, cf)| cf)
        .collect();
    let label = if keys {
        "cloudfront_keys"
    } else {
        "cloudfront_waf"
    };
    if let (Some(name), true) = (name, selected.is_empty()) {
        bail!("{label} '{name}' is not configured");
    }
    let sdk = crate::aws_util::load_sdk().await;
    let (stacks, store) = (AwsStacks { sdk: &sdk }, AwsSecretStore::new(sdk.clone()));
    let edge = EdgeDeploy {
        stacks: &stacks,
        store: &store,
        waits: EdgeWaits::default(),
    };
    for cf in selected {
        sink.info(&format!("[{}]", cf.name));
        if keys {
            edge.keys_delete(cf, sink).await?;
        } else {
            edge.waf_delete(cf, sink).await?;
        }
    }
    sink.success(&format!("{label} was deleted successfully."));
    Ok(())
}

// ---- inbound ----

pub async fn inbound_destroy(
    stage: &str,
    name: &str,
    yes: bool,
    sink: &mut dyn Sink,
) -> Result<()> {
    let (context, _) = load_context(stage)?;
    let Some(inlet) = context.inbound.get(name) else {
        bail!("inbound.{name} がありません");
    };
    confirm(
        "受信停止後36時間以上経過し、原本の照合・必要な退避を終えましたか？",
        yes,
    )?;
    let sdk = crate::aws_util::load_sdk().await;
    let (stacks, aws) = (AwsStacks { sdk: &sdk }, AwsInbound { sdk: &sdk });
    inbound_deploy(&stacks, &aws).delete(inlet).await?;
    let domain = &inlet.config.domain;
    for (other_name, other) in context.inbound.iter() {
        if other_name == name || &other.config.domain != domain {
            continue;
        }
        let description = stacks.describe(&other.region, &other.resource_name).await?;
        if cfn_status(description.as_ref())? != ResourceStatus::NoExist {
            sink.print(&format!(
                "inbound.{other_name} が {domain} を使用中のため、domainは残します"
            ));
            return Ok(());
        }
    }
    let Some(domain_context) = context.inbound_domain.get(domain) else {
        bail!("inbound domain {domain} がありません");
    };
    let aws = AwsInboundDomain { sdk: &sdk };
    inbound_domain_deploy(&stacks, &aws)
        .delete(domain_context)
        .await
}

#[cfg(test)]
mod tests {
    use super::*;

    fn map(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    #[test]
    fn test_reloaded_env_overlays_fresh_values() {
        let current = map(&[("A", "1"), ("B", "old"), ("KEEP", "x")]);
        let fresh = map(&[("A", "1"), ("B", "new"), ("C", "3")]);
        let (env, changed) = reloaded_env(&current, &fresh);
        assert_eq!(
            env,
            map(&[("A", "1"), ("B", "new"), ("C", "3"), ("KEEP", "x")])
        );
        assert_eq!(changed, ["B", "C"]);
        let (same, none) = reloaded_env(&env, &fresh);
        assert_eq!(same, env);
        assert!(none.is_empty());
    }

    #[test]
    fn test_stale_keys_are_sorted_and_exclude_local() {
        let local: BTreeMap<String, std::path::PathBuf> = [
            ("static/a.js".to_string(), "a.js".into()),
            ("static/b.js".to_string(), "b.js".into()),
        ]
        .into_iter()
        .collect();
        let existing = vec![
            "static/z.old".to_string(),
            "static/a.js".to_string(),
            "static/c.old".to_string(),
        ];
        assert_eq!(
            stale_keys(&existing, &local),
            ["static/c.old", "static/z.old"]
        );
    }
}
