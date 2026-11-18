//! deploy / promote の手順 (Python 版 `cli/deploy_cli.py` の `_deploy_pipeline`)。
//!
//! 手順の順と分岐だけをここに置き、AWS への操作は [`Ops`] の裏に置く
//! (順をテストで固定するため)。本番の実装は [`super::run`] にある

use anyhow::{bail, Result};
use serde_json::{json, Value as Json};

use crate::stack::ops::ResourceStatus;
use crate::status::resource::Resource;
use crate::status::sink::Sink;

/// リソースごとでない手順
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Phase {
    /// inbound / inbound-domain の deploy 前の検査
    InboundPrepare,
    /// DEPLOY_HASH の解決結果の表示
    DeployHash,
    /// 外部で作った IAM role の検査 (`external_roles`)
    VerifyRoles,
    /// state bucket の確保 (CodeBuild が source の置き場に使うので build より先)
    EnsureStateBucket,
    /// 宣言から消えた inbound の stack が残っていれば止める
    CheckRemovedInbound,
    /// versioning のある route の upload (Lambda が新しい版の URL を出す前)
    VersionedFrontend,
    /// どの inbound も使わなくなった受信ドメインの stack の削除
    CleanupInboundDomains,
    /// リリースまたぎの移行の掃除 (旧配置の削除)
    MigrationCleanup,
    ManagedAssets,
    /// 残りの route の upload
    Frontend,
    /// build context の読めないファイルの警告の再掲
    WorldReadWarnings,
    /// DLQ alert の購読が未確認なら警告
    AlertWarnings,
    InboundDetails,
    /// deploy 先の URL の表示
    Url,
    /// 受信ドメインの検証待ち (timeout しても他の結果は確定済みなので最後)
    ConfirmInboundDomains,
}

/// deploy の手順が使う操作。`index` は [`Ops::names`] の位置
pub trait Ops {
    /// リソースの class 名 (deploy の順)
    fn names(&self) -> Vec<&'static str>;
    async fn phase(&mut self, phase: Phase, sink: &mut dyn Sink) -> Result<()>;
    async fn deploy_init(&mut self, index: usize, sink: &mut dyn Sink) -> Result<()>;
    async fn status(&mut self, index: usize, sink: &mut dyn Sink) -> Result<ResourceStatus>;
    async fn create(&mut self, index: usize, sink: &mut dyn Sink) -> Result<()>;
    async fn update(&mut self, index: usize, sink: &mut dyn Sink) -> Result<()>;
    /// create / update が成功したリソースを state に記録する
    async fn record(&mut self, index: usize) -> Result<()>;
    /// stack の外の状態 (bucket policy・KVS・endpoint の公開など) を合わせ直す
    async fn ensure_post_deploy_state(&mut self, index: usize, sink: &mut dyn Sink) -> Result<()>;
}

#[derive(Debug, Clone, Copy, Default)]
pub struct DeployOptions {
    /// route の upload をしない
    pub skip_frontend: bool,
}

pub async fn run(ops: &mut impl Ops, options: DeployOptions, sink: &mut dyn Sink) -> Result<()> {
    for phase in [
        Phase::InboundPrepare,
        Phase::DeployHash,
        Phase::VerifyRoles,
        Phase::EnsureStateBucket,
        Phase::CheckRemovedInbound,
    ] {
        ops.phase(phase, sink).await?;
    }
    let names = ops.names();
    for (index, name) in names.iter().enumerate() {
        sink.log(&format!("Deploy init {name}..."));
        ops.deploy_init(index, sink).await?;
    }
    if !options.skip_frontend {
        ops.phase(Phase::VersionedFrontend, sink).await?;
    }
    for (index, name) in names.iter().enumerate() {
        match ops.status(index, sink).await? {
            ResourceStatus::NoExist => {
                sink.log(&format!("Creating {name}..."));
                ops.create(index, sink).await?;
                ops.record(index).await?;
            }
            ResourceStatus::RequireUpdate => {
                sink.log(&format!("Updating {name}..."));
                ops.update(index, sink).await?;
                ops.record(index).await?;
            }
            // 黙って飛ばすと、deploy が成功の表示で終わってしまう
            ResourceStatus::Failed => bail!(
                "{name} が FAILED 状態です。AWS コンソール等で状態を確認して\
                 解消してから再実行してください。"
            ),
            ResourceStatus::Progress => {
                bail!("{name} の前回操作が進行中です。完了を待ってから再実行してください。")
            }
            ResourceStatus::Completed => {
                sink.log(&format!("{name} is already the latest version."));
            }
        }
    }
    // stack の完了待ちが timeout した次の deploy でも復旧できるよう、毎回合わせ直す
    for index in 0..names.len() {
        ops.ensure_post_deploy_state(index, sink).await?;
    }
    ops.phase(Phase::CleanupInboundDomains, sink).await?;
    ops.phase(Phase::MigrationCleanup, sink).await?;
    ops.phase(Phase::ManagedAssets, sink).await?;
    if !options.skip_frontend {
        ops.phase(Phase::Frontend, sink).await?;
    }
    for phase in [
        Phase::WorldReadWarnings,
        Phase::AlertWarnings,
        Phase::InboundDetails,
        Phase::Url,
        Phase::ConfirmInboundDomains,
    ] {
        ops.phase(phase, sink).await?;
    }
    Ok(())
}

/// state に記録する内容 (Python 版の各リソースの `state_info`)。
///
/// `extra` は AWS から引く値: Rds の security group の id、Dsql の cluster の identifier
pub fn state_info(resource: &Resource<'_>, extra: Option<&str>) -> Json {
    match resource {
        Resource::CloudFrontAcm(cf) => {
            json!({format!("cloudfront-acm-{}", cf.name): {"domain": cf.domain}})
        }
        Resource::CloudFrontWaf(cf) => {
            json!({format!("cloudfront-waf-{}", cf.name): {"name": cf.name}})
        }
        Resource::InboundDomain(domain) => {
            json!({"inbound_domain": {domain.domain.clone(): {"stack_name": domain.stack_name}}})
        }
        Resource::Neon(neon) => json!({
            "neon": {"project_name": neon.project_name, "branch_name": neon.branch_name}
        }),
        // Python 版の TiDb は state に何も書かない
        Resource::TiDb(_) | Resource::Backup(_) => json!({}),
        Resource::Upstash(upstash) => {
            json!({"upstash": {"database_name": upstash.database_name}})
        }
        Resource::S3(s3) => json!({"s3": {"bucket_name": s3.bucket_name}}),
        Resource::Vpc(vpc) => match vpc.efs.as_ref() {
            Some(efs) => json!({"efs": {"name": efs.name}}),
            None => json!({}),
        },
        Resource::Dsql(dsql) => json!({
            "dsql": {
                "tag_name": dsql.tag_name,
                "identifier": extra,
                "endpoint": extra.map(|id| super::dsql::endpoint(dsql, id)),
                "endpoint_secret_name": dsql.endpoint_secret_name,
            }
        }),
        Resource::Rds(rds) if rds.managed => json!({
            "rds": {
                "managed": true,
                "cluster_identifier": rds.cluster_identifier,
                "security_group_id": extra,
            }
        }),
        Resource::Rds(rds) => json!({
            "rds": {
                "managed": false,
                "secret_arn": rds.secret_arn,
                "security_group_id": rds.security_group_id,
            }
        }),
        Resource::CloudFrontKeys(cf) => {
            json!({format!("cloudfront-keys-{}", cf.name): {"signing_key": cf.signing_key}})
        }
        Resource::Container(c) => {
            json!({"ecr": {c.name.clone(): {"repository_name": c.ecr_name}}})
        }
        Resource::Inbound(inlet) => {
            json!({"inbound": {inlet.name.clone(): {"stack_name": inlet.resource_name}}})
        }
        Resource::CloudFront(cf) => {
            json!({format!("cloudfront-{}", cf.name): {"bucket_name": cf.bucket_name}})
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stack::testing::vector_context;
    use crate::status::resource::resources;
    use crate::status::sink::{Recorded, Style};

    /// 呼ばれた順を記録する [`Ops`]
    struct FakeOps {
        names: Vec<&'static str>,
        statuses: Vec<ResourceStatus>,
        calls: Vec<String>,
        fail_on: Option<&'static str>,
    }

    impl FakeOps {
        fn new(resources: &[(&'static str, ResourceStatus)]) -> Self {
            Self {
                names: resources.iter().map(|(name, _)| *name).collect(),
                statuses: resources.iter().map(|(_, status)| *status).collect(),
                calls: Vec::new(),
                fail_on: None,
            }
        }

        fn call(&mut self, call: String) -> Result<()> {
            if self.fail_on.is_some_and(|target| target == call) {
                bail!("{call} failed");
            }
            self.calls.push(call);
            Ok(())
        }
    }

    impl Ops for FakeOps {
        fn names(&self) -> Vec<&'static str> {
            self.names.clone()
        }
        async fn phase(&mut self, phase: Phase, _sink: &mut dyn Sink) -> Result<()> {
            self.call(format!("{phase:?}"))
        }
        async fn deploy_init(&mut self, index: usize, _sink: &mut dyn Sink) -> Result<()> {
            self.call(format!("init {}", self.names[index]))
        }
        async fn status(&mut self, index: usize, _sink: &mut dyn Sink) -> Result<ResourceStatus> {
            Ok(self.statuses[index])
        }
        async fn create(&mut self, index: usize, _sink: &mut dyn Sink) -> Result<()> {
            self.call(format!("create {}", self.names[index]))
        }
        async fn update(&mut self, index: usize, _sink: &mut dyn Sink) -> Result<()> {
            self.call(format!("update {}", self.names[index]))
        }
        async fn record(&mut self, index: usize) -> Result<()> {
            self.call(format!("record {}", self.names[index]))
        }
        async fn ensure_post_deploy_state(
            &mut self,
            index: usize,
            _sink: &mut dyn Sink,
        ) -> Result<()> {
            self.call(format!("post {}", self.names[index]))
        }
    }

    fn three() -> FakeOps {
        FakeOps::new(&[
            ("S3", ResourceStatus::NoExist),
            ("Container", ResourceStatus::RequireUpdate),
            ("CloudFront", ResourceStatus::Completed),
        ])
    }

    #[tokio::test]
    async fn test_pipeline_order_matches_python() {
        let mut ops = three();
        let mut sink = Recorded::default();
        run(&mut ops, DeployOptions::default(), &mut sink)
            .await
            .unwrap();
        assert_eq!(
            ops.calls,
            [
                "InboundPrepare",
                "DeployHash",
                "VerifyRoles",
                "EnsureStateBucket",
                "CheckRemovedInbound",
                "init S3",
                "init Container",
                "init CloudFront",
                "VersionedFrontend",
                // create / update が成功したものだけ state に記録する。変更不要は記録しない
                "create S3",
                "record S3",
                "update Container",
                "record Container",
                "post S3",
                "post Container",
                "post CloudFront",
                "CleanupInboundDomains",
                "MigrationCleanup",
                "ManagedAssets",
                "Frontend",
                "WorldReadWarnings",
                "AlertWarnings",
                "InboundDetails",
                "Url",
                "ConfirmInboundDomains",
            ]
        );
        let logs: Vec<&str> = sink
            .0
            .iter()
            .filter(|(_, style, _)| *style == Style::Log)
            .map(|(_, _, text)| text.as_str())
            .collect();
        assert_eq!(
            logs,
            [
                "Deploy init S3...",
                "Deploy init Container...",
                "Deploy init CloudFront...",
                "Creating S3...",
                "Updating Container...",
                "CloudFront is already the latest version.",
            ]
        );
    }

    #[tokio::test]
    async fn test_skip_frontend_skips_only_route_uploads() {
        let mut ops = three();
        let options = DeployOptions {
            skip_frontend: true,
        };
        run(&mut ops, options, &mut Recorded::default())
            .await
            .unwrap();
        assert!(!ops.calls.iter().any(|c| c == "VersionedFrontend"));
        assert!(!ops.calls.iter().any(|c| c == "Frontend"));
        assert!(ops.calls.iter().any(|c| c == "ManagedAssets"));
    }

    #[tokio::test]
    async fn test_failed_or_in_progress_resource_stops_the_deploy() {
        for (status, want) in [
            (
                ResourceStatus::Failed,
                "Container が FAILED 状態です。AWS コンソール等で状態を確認して\
                 解消してから再実行してください。",
            ),
            (
                ResourceStatus::Progress,
                "Container の前回操作が進行中です。完了を待ってから再実行してください。",
            ),
        ] {
            let mut ops = FakeOps::new(&[
                ("S3", ResourceStatus::NoExist),
                ("Container", status),
                ("CloudFront", ResourceStatus::NoExist),
            ]);
            let error = run(&mut ops, DeployOptions::default(), &mut Recorded::default())
                .await
                .unwrap_err();
            assert_eq!(error.to_string(), want);
            // 手前のリソースは作って記録済み。後ろのリソースと後始末には進まない
            assert_eq!(ops.calls.last().unwrap(), "record S3");
        }
    }

    #[tokio::test]
    async fn test_failed_write_is_not_recorded() {
        let mut ops = three();
        ops.fail_on = Some("update Container");
        let error = run(&mut ops, DeployOptions::default(), &mut Recorded::default())
            .await
            .unwrap_err();
        assert_eq!(error.to_string(), "update Container failed");
        assert!(!ops.calls.iter().any(|c| c == "record Container"));
        assert!(!ops.calls.iter().any(|c| c.starts_with("post ")));
    }

    #[test]
    fn test_state_info_matches_python_shapes() {
        let ctx = vector_context("example/example-dsql.sandbox");
        let list = resources(&ctx);
        let mut merged = crate::deploy::state::empty_state();
        for resource in &list {
            let extra = match resource {
                Resource::Dsql(_) => Some("abc123"),
                _ => None,
            };
            crate::deploy::state::deep_merge(
                &mut merged["resources"],
                &state_info(resource, extra),
            );
        }
        let saved = &merged["resources"];
        let inlet = ctx.inbound.values().next().unwrap();
        let domain = ctx.inbound_domain.values().next().unwrap();
        assert_eq!(
            saved["inbound"][&inlet.name]["stack_name"],
            json!(inlet.resource_name)
        );
        assert_eq!(
            saved["inbound_domain"][&domain.domain]["stack_name"],
            json!(domain.stack_name)
        );
        let dsql = ctx.dsql.as_ref().unwrap();
        assert_eq!(
            saved["dsql"],
            json!({
                "tag_name": dsql.tag_name,
                "identifier": "abc123",
                "endpoint": format!("abc123.dsql.{}.on.aws", dsql.region),
                "endpoint_secret_name": dsql.endpoint_secret_name,
            })
        );
        let container = ctx.container.values().next().unwrap();
        assert_eq!(
            saved["ecr"][&container.name],
            json!({"repository_name": container.ecr_name})
        );
        let cf = ctx.cloudfront.values().next().unwrap();
        assert_eq!(
            saved[format!("cloudfront-{}", cf.name)],
            json!({"bucket_name": cf.bucket_name})
        );
        assert_eq!(
            saved["s3"]["bucket_name"],
            json!(ctx.s3.as_ref().unwrap().bucket_name)
        );
    }
}
