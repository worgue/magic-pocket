//! stack の状態の判定と操作 (Python 版 `Stack` の status / wait_status / create /
//! update / delete)。
//!
//! changeset は使わない。`create_stack` / `update_stack` を直接呼び、完了は
//! `describe_stacks` の polling で待つ (Python 版と同じ)。
//!
//! 状態の判定と待機の進め方は [`StackApi`] の上に書いてあり、AWS を呼ばずに
//! テストできる。

// 作成・更新・削除と完了待ちは deploy / destroy の移植で使う (今は `status` が読み取り側
// だけを使う)。deploy が入ったら外す
#![allow(dead_code)]

use std::io::Write as _;
use std::time::Duration;

use anyhow::{anyhow, bail, Result};
use aws_sdk_cloudformation::error::{ProvideErrorMetadata, SdkError};
use aws_sdk_cloudformation::types::{Capability, Tag};

use super::{StackOutputs, StackTemplate};

/// template の hash を載せる stack のタグ
pub const TEMPLATE_HASH_TAG: &str = "pocket:template_hash";

/// リソースの状態 (Python 版 `ResourceStatus`)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ResourceStatus {
    NoExist,
    RequireUpdate,
    Progress,
    Completed,
    Failed,
}

impl ResourceStatus {
    /// Python 版の文字列 (`pocket status` の表示に使う)
    pub fn as_str(self) -> &'static str {
        match self {
            Self::NoExist => "NOEXIST",
            Self::RequireUpdate => "REQUIRE_UPDATE",
            Self::Progress => "PROGRESS",
            Self::Completed => "COMPLETED",
            Self::Failed => "FAILED",
        }
    }
}

impl std::fmt::Display for ResourceStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// `describe_stacks` の 1 件のうち、pocket が使う部分
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct StackDescription {
    pub stack_id: String,
    /// `StackStatus` (`CREATE_COMPLETE` 等)
    pub status: String,
    pub status_reason: Option<String>,
    pub outputs: Vec<StackOutput>,
    pub tags: Vec<(String, String)>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StackOutput {
    pub key: String,
    pub value: String,
    pub export_name: Option<String>,
}

/// `list_stack_resources` の 1 件
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StackResource {
    pub logical_id: String,
    /// `AWS::EC2::VPC` など
    pub resource_type: String,
    pub physical_id: Option<String>,
    pub status: String,
    pub status_reason: Option<String>,
}

/// 生の状態名。stack が無ければ `NOT_CREATED`
pub fn status_detail(description: Option<&StackDescription>) -> &str {
    description.map_or("NOT_CREATED", |d| d.status.as_str())
}

/// CloudFormation の状態だけで判定する (template の同期は見ない)。
/// stack 操作の完了待ちに使う
pub fn cfn_status(description: Option<&StackDescription>) -> Result<ResourceStatus> {
    let detail = status_detail(description);
    if detail == "NOT_CREATED" {
        return Ok(ResourceStatus::NoExist);
    }
    let action = detail.split('_').next().unwrap_or_default();
    match detail.rsplit('_').next().unwrap_or_default() {
        "PROGRESS" => return Ok(ResourceStatus::Progress),
        "FAILED" => return Ok(ResourceStatus::Failed),
        "COMPLETE" => match action {
            "DELETE" => return Ok(ResourceStatus::NoExist),
            // 作成の失敗で rollback した stack。update できず、消すしかない
            "ROLLBACK" => return Ok(ResourceStatus::Failed),
            // UPDATE_ROLLBACK_COMPLETE もここで COMPLETED になる (安定状態で再 update
            // できるため。rollback でタグの template hash が旧値に戻るので status は
            // REQUIRE_UPDATE になる)。更新の失敗は wait_status が ROLLBACK への遷移で検出する
            "IMPORT" | "REVIEW" | "UPDATE" | "CREATE" => return Ok(ResourceStatus::Completed),
            _ => {}
        },
        _ => {}
    }
    bail!("unknown status: {detail}")
}

/// deploy 済みの stack のタグにある template hash
pub fn deployed_template_hash(description: Option<&StackDescription>) -> Option<&str> {
    description?
        .tags
        .iter()
        .find(|(key, _)| key == TEMPLATE_HASH_TAG)
        .map(|(_, value)| value.as_str())
}

/// deploy 済みの template が `stack` と同じか (Python 版 `yaml_synced`)。
/// タグが無い stack (`pocket migrate` 前) は案内を出して false
pub fn template_synced(
    description: Option<&StackDescription>,
    stack: &StackTemplate,
) -> Result<bool> {
    let Some(deployed) = deployed_template_hash(description) else {
        if cfn_status(description)? != ResourceStatus::NoExist {
            println!(
                "pocket:template_hash タグがありません: {}\n\
                 pocket migrate --stage=<stage> を実行してください。",
                stack.name
            );
        }
        return Ok(false);
    };
    Ok(deployed == stack.template_hash())
}

/// deploy で使う状態。COMPLETED でも template が同期していなければ REQUIRE_UPDATE
pub fn stack_status(
    description: Option<&StackDescription>,
    stack: &StackTemplate,
) -> Result<ResourceStatus> {
    let cfn = cfn_status(description)?;
    if cfn == ResourceStatus::Completed && !template_synced(description, stack)? {
        return Ok(ResourceStatus::RequireUpdate);
    }
    Ok(cfn)
}

/// stack の Outputs (OutputKey と Export 名の両方で引ける)。stack か Outputs が無ければ `None`
pub fn stack_outputs(description: Option<&StackDescription>) -> Option<StackOutputs> {
    let description = description.filter(|d| !d.outputs.is_empty())?;
    let mut result = StackOutputs::new();
    for output in &description.outputs {
        result.insert(output.key.clone(), output.value.clone());
        if let Some(export) = &output.export_name {
            result.insert(export.clone(), output.value.clone());
        }
    }
    Some(result)
}

/// stack の読み取り (polling と失敗の要約が使う)
pub trait StackApi {
    /// `describe_stacks`。stack が無ければ `None`
    async fn describe(&self, name: &str) -> Result<Option<StackDescription>>;

    /// `list_stack_resources` (全ページ)
    async fn resources(&self, name: &str) -> Result<Vec<StackResource>>;
}

/// 失敗の理由だけを並べる (設定値を含む stack 全体の dump は出さない)
pub async fn failure_summary(
    api: &impl StackApi,
    name: &str,
    description: Option<&StackDescription>,
) -> String {
    let mut lines = vec![format!("Stack '{name}': {}", status_detail(description))];
    if let Some(reason) = description
        .and_then(|d| d.status_reason.as_deref())
        .filter(|r| !r.is_empty())
    {
        lines.push(reason.to_string());
    }
    match api.resources(name).await {
        Err(error) => lines.push(format!("リソース詳細を取得できません: {error:#}")),
        Ok(resources) => {
            for resource in resources.iter().filter(|r| r.status.ends_with("FAILED")) {
                lines.push(format!(
                    "{} ({}, {}): {}",
                    resource.logical_id,
                    resource.physical_id.as_deref().unwrap_or("-"),
                    resource.status,
                    resource.status_reason.as_deref().unwrap_or("理由なし"),
                ));
            }
        }
    }
    lines.join("\n")
}

/// 待機の回数と間隔
#[derive(Debug, Clone, Copy)]
pub struct WaitOptions {
    pub attempts: u64,
    pub interval: Duration,
    /// timeout のメッセージに出す秒数
    pub timeout_secs: u64,
}

impl WaitOptions {
    /// `timeout_secs` 秒を `interval_secs` 秒おきに見る (Python: `timeout // interval` 回)
    pub fn new(timeout_secs: u64, interval_secs: u64) -> Self {
        Self {
            attempts: timeout_secs / interval_secs.max(1),
            interval: Duration::from_secs(interval_secs),
            timeout_secs,
        }
    }
}

impl Default for WaitOptions {
    fn default() -> Self {
        Self::new(300, 3)
    }
}

/// stack が `target` になるまで待つ。
///
/// - `label`: 進行表示に出す名前 (Python 版は template のファイル名)
/// - 待機開始後に ROLLBACK 系へ「遷移」したら操作の失敗として止める
///   (UPDATE_ROLLBACK_COMPLETE 自体は COMPLETED なので、遷移を見ないと更新の失敗が
///   成功扱いになる)。最初から ROLLBACK 系の stack は失敗と見なさない
/// - FAILED になったら失敗の要約を付けて止める
/// - COMPLETED を待っているのに stack が 3 回続けて見つからなければ止める
pub async fn wait_status(
    api: &impl StackApi,
    name: &str,
    label: &str,
    target: ResourceStatus,
    options: WaitOptions,
) -> Result<()> {
    let initial = api.describe(name).await?;
    let initial_detail = status_detail(initial.as_ref()).to_string();
    let mut noexist_count = 0;
    for i in 0..options.attempts {
        let description = api.describe(name).await?;
        let description = description.as_ref();
        let current = cfn_status(description)?;
        let detail = status_detail(description);
        if target == ResourceStatus::Completed
            && detail.contains("ROLLBACK")
            && detail != initial_detail
        {
            println!();
            bail!(
                "Stack operation was rolled back ({detail}). {}",
                failure_summary(api, name, description).await
            );
        }
        if current == target {
            println!();
            return Ok(());
        }
        if current == ResourceStatus::Failed {
            bail!("{}", failure_summary(api, name, description).await);
        }
        if target != ResourceStatus::NoExist && current == ResourceStatus::NoExist {
            noexist_count += 1;
            if noexist_count >= 3 {
                bail!(
                    "Stack '{name}' が見つかりません。作成に失敗した可能性があります。\
                     AWS コンソールで権限とリージョンを確認してください。"
                );
            }
        } else {
            noexist_count = 0;
        }
        if i == 0 {
            print!("Waiting for {label} stack status to be {target}");
        }
        print!(".");
        std::io::stdout().flush().ok();
        tokio::time::sleep(options.interval).await;
    }
    let mut message = format!(
        "Stack '{name}' の待機が {} 秒でタイムアウトしました。",
        options.timeout_secs
    );
    if target == ResourceStatus::NoExist {
        message.push_str(
            "AWS 側の削除は継続している可能性があります。\
             状態を確認し、同じ destroy コマンドを再実行してください。",
        );
    }
    bail!(message)
}

/// CloudFormation の client (stack の region ごとに作る)
pub struct CfnClient {
    client: aws_sdk_cloudformation::Client,
}

impl CfnClient {
    pub fn new(sdk: &aws_config::SdkConfig, region: &str) -> Self {
        let config = aws_sdk_cloudformation::config::Builder::from(sdk)
            .region(aws_config::Region::new(region.to_string()))
            .build();
        Self {
            client: aws_sdk_cloudformation::Client::from_conf(config),
        }
    }

    pub async fn create(&self, stack: &StackTemplate) -> Result<()> {
        self.client
            .create_stack()
            .stack_name(&stack.name)
            .template_body(&stack.body)
            .set_capabilities(Some(capabilities(stack)))
            .set_tags(Some(tags(stack)))
            .send()
            .await
            .map_err(|e| anyhow!("create_stack {}: {}", stack.name, error_text(&e)))?;
        Ok(())
    }

    pub async fn update(&self, stack: &StackTemplate) -> Result<()> {
        println!("Update stack");
        self.client
            .update_stack()
            .stack_name(&stack.name)
            .template_body(&stack.body)
            .set_capabilities(Some(capabilities(stack)))
            .set_tags(Some(tags(stack)))
            .send()
            .await
            .map_err(|e| anyhow!("update_stack {}: {}", stack.name, error_text(&e)))?;
        Ok(())
    }

    /// deploy 済みの template のテキスト。stack が無ければ `None`
    pub async fn template(&self, name: &str) -> Result<Option<String>> {
        match self.client.get_template().stack_name(name).send().await {
            Ok(response) => Ok(response.template_body().map(str::to_string)),
            Err(e) if is_stack_not_exist(e.code(), e.message()) => Ok(None),
            Err(e) => bail!("get_template {name}: {}", error_text(&e)),
        }
    }

    pub async fn delete(&self, name: &str) -> Result<()> {
        self.client
            .delete_stack()
            .stack_name(name)
            .send()
            .await
            .map_err(|e| anyhow!("delete_stack {name}: {}", error_text(&e)))?;
        Ok(())
    }
}

impl StackApi for CfnClient {
    async fn describe(&self, name: &str) -> Result<Option<StackDescription>> {
        let response = match self.client.describe_stacks().stack_name(name).send().await {
            Ok(response) => response,
            Err(e) if is_stack_not_exist(e.code(), e.message()) => return Ok(None),
            Err(e) => bail!("describe_stacks {name}: {}", error_text(&e)),
        };
        let Some(stack) = response.stacks().first() else {
            return Ok(None);
        };
        Ok(Some(StackDescription {
            stack_id: stack.stack_id().unwrap_or_default().to_string(),
            status: stack
                .stack_status()
                .map(|s| s.as_str().to_string())
                .unwrap_or_default(),
            status_reason: stack.stack_status_reason().map(str::to_string),
            outputs: stack
                .outputs()
                .iter()
                .map(|o| StackOutput {
                    key: o.output_key().unwrap_or_default().to_string(),
                    value: o.output_value().unwrap_or_default().to_string(),
                    export_name: o.export_name().map(str::to_string),
                })
                .collect(),
            tags: stack
                .tags()
                .iter()
                .map(|t| {
                    (
                        t.key().unwrap_or_default().to_string(),
                        t.value().unwrap_or_default().to_string(),
                    )
                })
                .collect(),
        }))
    }

    async fn resources(&self, name: &str) -> Result<Vec<StackResource>> {
        let mut pages = self
            .client
            .list_stack_resources()
            .stack_name(name)
            .into_paginator()
            .send();
        let mut resources = Vec::new();
        while let Some(page) = pages.next().await {
            let page =
                page.map_err(|e| anyhow!("list_stack_resources {name}: {}", error_text(&e)))?;
            for summary in page.stack_resource_summaries() {
                resources.push(StackResource {
                    logical_id: summary
                        .logical_resource_id()
                        .unwrap_or_default()
                        .to_string(),
                    resource_type: summary.resource_type().unwrap_or_default().to_string(),
                    physical_id: summary.physical_resource_id().map(str::to_string),
                    status: summary
                        .resource_status()
                        .map(|s| s.as_str().to_string())
                        .unwrap_or_default(),
                    status_reason: summary.resource_status_reason().map(str::to_string),
                });
            }
        }
        Ok(resources)
    }
}

/// describe_stacks の「stack が無い」エラーか。throttling / AccessDenied まで「無い」に
/// すると、既存 stack を NOEXIST と誤判定する (create_stack の AlreadyExists) ので、
/// エラーの種別と文言で判定する
fn is_stack_not_exist(code: Option<&str>, message: Option<&str>) -> bool {
    code == Some("ValidationError") && message.is_some_and(|m| m.contains("does not exist"))
}

fn error_text<E: ProvideErrorMetadata + std::fmt::Debug, R: std::fmt::Debug>(
    error: &SdkError<E, R>,
) -> String {
    match (error.code(), error.message()) {
        (Some(code), Some(message)) => format!("{code}: {message}"),
        _ => format!("{error:?}"),
    }
}

fn capabilities(stack: &StackTemplate) -> Vec<Capability> {
    stack
        .capabilities
        .iter()
        .map(|c| Capability::from(c.as_str()))
        .collect()
}

/// stack のタグ (stack 固有のタグ + template hash)
fn tags(stack: &StackTemplate) -> Vec<Tag> {
    let mut pairs = stack.tags.clone();
    pairs.push((TEMPLATE_HASH_TAG.to_string(), stack.template_hash()));
    pairs
        .into_iter()
        .map(|(key, value)| Tag::builder().key(key).value(value).build())
        .collect()
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;
    use std::collections::VecDeque;

    use serde_json::Map;

    use super::*;

    fn description(status: &str) -> StackDescription {
        StackDescription {
            status: status.to_string(),
            ..Default::default()
        }
    }

    fn template(body: &str) -> StackTemplate {
        StackTemplate {
            name: "dev-prj-test".to_string(),
            region: "ap-northeast-1".to_string(),
            capabilities: Vec::new(),
            tags: vec![("pocket:sharable".to_string(), "true".to_string())],
            export: Map::new(),
            body: body.to_string(),
        }
    }

    /// describe が順に返す状態を並べた fake (最後の状態は繰り返す)
    struct FakeApi {
        states: RefCell<VecDeque<Option<StackDescription>>>,
        resources: Vec<StackResource>,
    }

    impl FakeApi {
        fn new(states: &[Option<&str>]) -> Self {
            Self {
                states: RefCell::new(states.iter().map(|s| s.map(description)).collect()),
                resources: Vec::new(),
            }
        }
    }

    impl StackApi for FakeApi {
        async fn describe(&self, _name: &str) -> Result<Option<StackDescription>> {
            let mut states = self.states.borrow_mut();
            if states.len() > 1 {
                return Ok(states.pop_front().expect("状態"));
            }
            Ok(states.front().cloned().expect("状態"))
        }

        async fn resources(&self, _name: &str) -> Result<Vec<StackResource>> {
            Ok(self.resources.clone())
        }
    }

    fn quick(attempts: u64) -> WaitOptions {
        WaitOptions {
            attempts,
            interval: Duration::ZERO,
            timeout_secs: 300,
        }
    }

    #[test]
    fn test_cfn_status() {
        use ResourceStatus::*;
        assert_eq!(cfn_status(None).unwrap(), NoExist);
        for (detail, want) in [
            ("CREATE_IN_PROGRESS", Progress),
            ("UPDATE_COMPLETE_CLEANUP_IN_PROGRESS", Progress),
            ("UPDATE_ROLLBACK_IN_PROGRESS", Progress),
            ("CREATE_FAILED", Failed),
            ("UPDATE_ROLLBACK_FAILED", Failed),
            ("DELETE_FAILED", Failed),
            ("CREATE_COMPLETE", Completed),
            ("UPDATE_COMPLETE", Completed),
            ("UPDATE_ROLLBACK_COMPLETE", Completed),
            ("IMPORT_COMPLETE", Completed),
            ("DELETE_COMPLETE", NoExist),
            ("ROLLBACK_COMPLETE", Failed),
        ] {
            assert_eq!(
                cfn_status(Some(&description(detail))).unwrap(),
                want,
                "{detail}"
            );
        }
        assert!(cfn_status(Some(&description("SOMETHING_ELSE"))).is_err());
    }

    #[test]
    fn test_template_hash_matches_python() {
        // hashlib.sha256(b"abc").hexdigest()[:16]
        assert_eq!(template("abc").template_hash(), "ba7816bf8f01cfea");
    }

    #[test]
    fn test_stack_status_requires_update_when_hash_differs() {
        let stack = template("abc");
        let mut deployed = description("UPDATE_COMPLETE");
        assert_eq!(
            stack_status(Some(&deployed), &stack).unwrap(),
            ResourceStatus::RequireUpdate,
            "タグが無ければ同期していない扱い"
        );
        deployed.tags = vec![(
            TEMPLATE_HASH_TAG.to_string(),
            "0000000000000000".to_string(),
        )];
        assert_eq!(
            stack_status(Some(&deployed), &stack).unwrap(),
            ResourceStatus::RequireUpdate
        );
        deployed.tags = vec![(TEMPLATE_HASH_TAG.to_string(), stack.template_hash())];
        assert_eq!(
            stack_status(Some(&deployed), &stack).unwrap(),
            ResourceStatus::Completed
        );
        // 進行中・失敗は template を見ない
        deployed.status = "UPDATE_IN_PROGRESS".to_string();
        assert_eq!(
            stack_status(Some(&deployed), &stack).unwrap(),
            ResourceStatus::Progress
        );
        assert_eq!(stack_status(None, &stack).unwrap(), ResourceStatus::NoExist);
    }

    #[test]
    fn test_tags_append_template_hash() {
        let stack = template("abc");
        let tags: Vec<(String, String)> = tags(&stack)
            .iter()
            .map(|t| {
                (
                    t.key().unwrap_or_default().to_string(),
                    t.value().unwrap_or_default().to_string(),
                )
            })
            .collect();
        assert_eq!(
            tags,
            [
                ("pocket:sharable".to_string(), "true".to_string()),
                (
                    TEMPLATE_HASH_TAG.to_string(),
                    "ba7816bf8f01cfea".to_string()
                ),
            ]
        );
    }

    #[test]
    fn test_stack_outputs_include_export_names() {
        assert_eq!(stack_outputs(None), None);
        let mut deployed = description("CREATE_COMPLETE");
        assert_eq!(stack_outputs(Some(&deployed)), None);
        deployed.outputs = vec![
            StackOutput {
                key: "VpcId".to_string(),
                value: "vpc-1".to_string(),
                export_name: Some("main-vpc-id".to_string()),
            },
            StackOutput {
                key: "Plain".to_string(),
                value: "x".to_string(),
                export_name: None,
            },
        ];
        let outputs = stack_outputs(Some(&deployed)).unwrap();
        assert_eq!(outputs["VpcId"], "vpc-1");
        assert_eq!(outputs["main-vpc-id"], "vpc-1");
        assert_eq!(outputs["Plain"], "x");
        assert_eq!(outputs.len(), 3);
    }

    #[test]
    fn test_is_stack_not_exist() {
        assert!(is_stack_not_exist(
            Some("ValidationError"),
            Some("Stack with id dev-x does not exist")
        ));
        assert!(!is_stack_not_exist(
            Some("Throttling"),
            Some("Rate exceeded")
        ));
        assert!(!is_stack_not_exist(
            Some("ValidationError"),
            Some("Template format error")
        ));
        assert!(!is_stack_not_exist(Some("AccessDenied"), None));
    }

    #[tokio::test]
    async fn test_wait_until_completed() {
        let api = FakeApi::new(&[
            Some("CREATE_IN_PROGRESS"),
            Some("CREATE_IN_PROGRESS"),
            Some("CREATE_COMPLETE"),
        ]);
        wait_status(&api, "s", "vpc", ResourceStatus::Completed, quick(5))
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn test_wait_detects_rollback_transition() {
        let api = FakeApi::new(&[
            Some("UPDATE_IN_PROGRESS"),
            Some("UPDATE_IN_PROGRESS"),
            Some("UPDATE_ROLLBACK_COMPLETE"),
        ]);
        let error = wait_status(&api, "s", "vpc", ResourceStatus::Completed, quick(5))
            .await
            .unwrap_err()
            .to_string();
        assert!(
            error.starts_with("Stack operation was rolled back (UPDATE_ROLLBACK_COMPLETE). Stack 's': UPDATE_ROLLBACK_COMPLETE"),
            "{error}"
        );
    }

    #[tokio::test]
    async fn test_wait_accepts_stack_resting_in_rollback() {
        // 最初から UPDATE_ROLLBACK_COMPLETE (依存先を待つだけ) なら失敗にしない
        let api = FakeApi::new(&[Some("UPDATE_ROLLBACK_COMPLETE")]);
        wait_status(&api, "s", "vpc", ResourceStatus::Completed, quick(5))
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn test_wait_reports_failed_resources() {
        let mut api = FakeApi::new(&[Some("CREATE_IN_PROGRESS"), Some("CREATE_FAILED")]);
        api.resources = vec![
            StackResource {
                logical_id: "Bucket".to_string(),
                resource_type: "AWS::S3::Bucket".to_string(),
                physical_id: None,
                status: "CREATE_FAILED".to_string(),
                status_reason: Some("already exists".to_string()),
            },
            StackResource {
                logical_id: "Topic".to_string(),
                resource_type: "AWS::SNS::Topic".to_string(),
                physical_id: Some("arn:topic".to_string()),
                status: "CREATE_COMPLETE".to_string(),
                status_reason: None,
            },
        ];
        let error = wait_status(&api, "s", "vpc", ResourceStatus::Completed, quick(5))
            .await
            .unwrap_err()
            .to_string();
        assert_eq!(
            error,
            "Stack 's': CREATE_FAILED\nBucket (-, CREATE_FAILED): already exists"
        );
    }

    #[tokio::test]
    async fn test_wait_stops_when_stack_never_appears() {
        let api = FakeApi::new(&[None]);
        let error = wait_status(&api, "s", "vpc", ResourceStatus::Completed, quick(10))
            .await
            .unwrap_err()
            .to_string();
        assert!(error.starts_with("Stack 's' が見つかりません。"), "{error}");
    }

    #[tokio::test]
    async fn test_wait_for_delete_and_timeout() {
        let api = FakeApi::new(&[Some("DELETE_IN_PROGRESS"), None]);
        wait_status(&api, "s", "vpc", ResourceStatus::NoExist, quick(5))
            .await
            .unwrap();

        let api = FakeApi::new(&[Some("DELETE_IN_PROGRESS")]);
        let error = wait_status(&api, "s", "vpc", ResourceStatus::NoExist, quick(2))
            .await
            .unwrap_err()
            .to_string();
        assert_eq!(
            error,
            "Stack 's' の待機が 300 秒でタイムアウトしました。AWS 側の削除は継続している\
             可能性があります。状態を確認し、同じ destroy コマンドを再実行してください。"
        );
    }
}
