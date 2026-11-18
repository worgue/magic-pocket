//! `pocket-rs destroy` の手順 (Python 版 `cli/destroy_cli.py`)。
//!
//! 削除対象を集めて一覧で見せ、確認してから deploy の逆順に消す。対象の文言と削除の順は
//! ここで決め、AWS への操作は [`DestroyOps`] の裏に置く (本番の実装は [`super::destroy_run`])。
//!
//! 途中で止まった destroy は、同じコマンドの再実行で続きから進む: どの手順も「在れば消す」
//! で、削除中の stack は完了を待つだけにする

use anyhow::{bail, Result};
use magic_pocket_settings::context::{ContainerContext, Context};
use magic_pocket_settings::model::ProvisioningMode;

use super::interaction::Prompt;
use super::migrations::{self, Repositories};
use super::neon::DestroyPlan;
use super::stacks::{self, Stacks};
use crate::stack::container as container_stack;
use crate::stack::ops::{cfn_status, status_detail, ResourceStatus, WaitOptions};
use crate::status::sink::Sink;

#[derive(Debug, Clone, Copy)]
pub struct DestroyOptions {
    /// managed secret も消す (`--without-secrets` で残す)
    pub with_secrets: bool,
    pub with_state_bucket: bool,
}

/// container 1 つの、今の状態
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ContainerFact {
    pub name: String,
    pub ecr_exists: bool,
    /// `ecr_name` を明示している (他の stage と共有しうるので消さない)
    pub ecr_name_overridden: bool,
}

/// container が使う VPC 1 つの、今の状態
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct VpcFact {
    pub manage: bool,
    pub stack_exists: bool,
    /// この VPC を使っている他の project (`pocket:consumer:` のタグ)
    pub consumers: Vec<String>,
    pub efs_exists: bool,
}

/// 削除対象の一覧を組むために AWS から集めた事実
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Facts {
    pub containers: Vec<ContainerFact>,
    pub legacy_stack: bool,
    pub legacy_ecr: bool,
    pub codebuild: bool,
    /// 在る backup plan の名前
    pub backup_plans: Vec<String>,
    pub dsql: bool,
    pub rds: bool,
    pub tidb: bool,
    pub upstash: bool,
    pub neon: Option<DestroyPlan>,
    pub vpcs: Vec<VpcFact>,
    pub s3: bool,
}

/// 削除対象の一覧 (確認の前に見せる文言。Python 版 `_collect_targets` と同じ順)
pub fn targets(ctx: &Context, facts: &Facts, options: DestroyOptions) -> Vec<String> {
    let mut list = Vec::new();
    for (name, cf) in ctx.cloudfront.iter() {
        list.push(format!(
            "CloudFront '{name}' (CFNスタック + バケットポリシー)"
        ));
        if cf.domain.as_deref().is_some_and(|d| !d.is_empty()) {
            list.push(format!("CloudFront ACM '{name}' (us-east-1 証明書)"));
        }
    }
    for name in ctx.inbound.keys() {
        list.push(format!("Inbound {name} (原本バケットと配送DLQは保持)"));
    }
    for domain in ctx.inbound_domain.keys() {
        list.push(format!(
            "Inbound domain {domain} (SES identity / DKIM / MX)"
        ));
    }
    if !ctx.container.is_empty() {
        for container in &facts.containers {
            let mut parts = vec!["CFNスタック"];
            if container.ecr_exists {
                parts.push(if container.ecr_name_overridden {
                    "ECR は ecr_name 明示指定のため削除対象外"
                } else {
                    "ECR"
                });
            }
            list.push(format!(
                "Container '{}' ({})",
                container.name,
                parts.join(" + ")
            ));
        }
        if facts.legacy_stack {
            list.push(format!(
                "旧形式の container stack '{}' (0.29.0 以前の [awscontainer] 命名。CFNスタック)",
                migrations::legacy_container_stack_name(ctx)
            ));
        }
        if facts.legacy_ecr {
            if let Ok(repo) = migrations::legacy_ecr_repo_name(ctx) {
                list.push(format!(
                    "旧形式の ECR repository '{repo}' (0.29.0 以前の命名)"
                ));
            }
        }
        let mut shared = Vec::new();
        let has_secrets =
            ctx.secrets.is_some() || ctx.container.values().any(|c| c.secrets.is_some());
        if options.with_secrets && has_secrets {
            shared.push("secrets");
        }
        if facts.codebuild {
            shared.push("CodeBuild");
        }
        if !shared.is_empty() {
            list.push(format!("Container 共有リソース ({})", shared.join(" + ")));
        }
    }
    for plan in &facts.backup_plans {
        list.push(format!(
            "AWS Backup plan: {plan} (recovery point と vault は残ります)"
        ));
    }
    if let Some(dsql) = ctx.dsql.as_ref().filter(|_| facts.dsql) {
        list.push(format!("DSQL クラスター: {}", dsql.tag_name));
    }
    if let Some(rds) = ctx.rds.as_ref().filter(|_| facts.rds) {
        list.push(format!("RDS Aurora クラスター: {}", rds.cluster_identifier));
    }
    if facts.tidb {
        list.push("TiDB クラスタ".to_string());
    }
    if let Some(upstash) = ctx.upstash.as_ref().filter(|_| facts.upstash) {
        list.push(format!("Upstash Redis: {}", upstash.database_name));
    }
    match (ctx.neon.as_ref(), facts.neon) {
        (Some(neon), Some(DestroyPlan::Project)) => list.push(format!(
            "Neon プロジェクト '{}' (root branch のため project ごと削除)",
            neon.project_name
        )),
        (Some(_), Some(DestroyPlan::Branch)) => list.push("Neon ブランチ".to_string()),
        // root branch で他の branch が同居: 消せないので一覧に載せない (実行時に知らせる)
        _ => {}
    }
    for vpc in &facts.vpcs {
        if !vpc.manage {
            list.push("VPC (外部 VPC consumer タグ削除)".to_string());
            continue;
        }
        let mut parts = Vec::new();
        if vpc.stack_exists {
            parts.push("CFNスタック".to_string());
            if !vpc.consumers.is_empty() {
                parts.push(format!("consumers: {}", vpc.consumers.join(", ")));
            }
        }
        if vpc.efs_exists {
            parts.push("EFS".to_string());
        }
        if !parts.is_empty() {
            list.push(format!("VPC ({})", parts.join(" + ")));
        }
    }
    for (name, cf) in ctx.cloudfront.iter() {
        if cf.signing_key.as_deref().is_some_and(|k| !k.is_empty()) {
            list.push(format!("CloudFrontKeys '{name}' (CFNスタック)"));
        }
    }
    if let Some(s3) = ctx.s3.as_ref().filter(|_| facts.s3) {
        list.push(format!("S3 バケット: {}", s3.bucket_name));
    }
    if options.with_state_bucket {
        list.push("ステートバケット".to_string());
    }
    list
}

/// 削除の手順 (deploy の逆順)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Step {
    Inbound,
    InboundDomain,
    CloudFrontAndAcm,
    Containers,
    LegacyContainers,
    CodeBuild,
    LogGroups,
    Secrets,
    /// plan / selection。DSQL / RDS の cluster より先 (cluster を消すと recovery point を
    /// ARN で引けなくなる)
    Backup,
    Dsql,
    /// container の後、VPC の前 (RDS が VPC の subnet と security group を使う)
    Rds,
    Vpc,
    CloudFrontKeys,
    S3,
    Tidb,
    Upstash,
    Neon,
    StateBucket,
}

/// 実行する手順の並び
pub fn steps(options: DestroyOptions) -> Vec<Step> {
    let mut steps = vec![
        Step::Inbound,
        Step::InboundDomain,
        Step::CloudFrontAndAcm,
        Step::Containers,
        Step::LegacyContainers,
        Step::CodeBuild,
        Step::LogGroups,
    ];
    if options.with_secrets {
        steps.push(Step::Secrets);
    }
    steps.extend([
        Step::Backup,
        Step::Dsql,
        Step::Rds,
        Step::Vpc,
        Step::CloudFrontKeys,
        Step::S3,
        Step::Tidb,
        Step::Upstash,
        Step::Neon,
    ]);
    if options.with_state_bucket {
        steps.push(Step::StateBucket);
    }
    steps
}

pub trait DestroyOps {
    /// 削除対象の一覧 (AWS から集めた事実を文言にしたもの)
    async fn targets(
        &mut self,
        options: DestroyOptions,
        sink: &mut dyn Sink,
    ) -> Result<Vec<String>>;
    async fn step(&mut self, step: Step, sink: &mut dyn Sink) -> Result<()>;
}

pub async fn run(
    ops: &mut impl DestroyOps,
    stage: &str,
    options: DestroyOptions,
    prompt: &impl Prompt,
    sink: &mut dyn Sink,
) -> Result<()> {
    let targets = ops.targets(options, sink).await?;
    if targets.is_empty() {
        sink.warning("削除対象のリソースが見つかりません。");
        return Ok(());
    }
    sink.danger("以下のリソースを削除します:");
    for target in &targets {
        sink.info(&format!("  - {target}"));
    }
    sink.danger("この操作は取り消せません！");
    let question = format!("stage '{stage}' の全リソースを削除しますか？");
    if !prompt.confirm(&question, false)? {
        bail!("Aborted!");
    }
    for step in steps(options) {
        ops.step(step, sink).await?;
    }
    sink.success(&format!("stage '{stage}' の全リソースを削除しました。"));
    Ok(())
}

/// container の stack の削除の完了待ち。Lambda が VPC の中にあると ENI の解放まで
/// 時間がかかり、待たずに進むと後の VPC の削除が失敗する
pub fn container_delete_wait() -> WaitOptions {
    WaitOptions::new(1800, 10)
}

/// container 1 つの stack と ECR repository を消す
pub async fn destroy_container(
    c: &ContainerContext,
    stacks: &impl Stacks,
    repos: &impl Repositories,
    wait: WaitOptions,
    sink: &mut dyn Sink,
) -> Result<()> {
    let name = container_stack::stack_name(c);
    let description = stacks.describe(&c.region, &name).await?;
    if cfn_status(description.as_ref())? != ResourceStatus::NoExist {
        sink.log(&format!("Destroying container '{}' stack...", c.name));
        // 削除中なら、前回の destroy が出した削除の完了を待つだけにする
        if status_detail(description.as_ref()) != "DELETE_IN_PROGRESS" {
            stacks.delete(&c.region, &name).await?;
        }
        let target = ResourceStatus::NoExist;
        stacks::wait(stacks, &c.region, &name, "container", target, wait).await?;
        sink.success(&format!("Container '{}' stack was destroyed.", c.name));
    }
    if repos.exists(&c.region, &c.ecr_name).await? {
        if c.ecr_name_overridden {
            sink.warning(&format!(
                "ECR repository '{}' は ecr_name で明示指定されているため削除\
                 しません (他 stage と共有の可能性があります)。\
                 不要な場合は手動で削除してください。",
                c.ecr_name
            ));
        } else {
            sink.log("Destroying ECR repository...");
            repos.delete(&c.region, &c.ecr_name).await?;
            sink.success("ECR repository was deleted.");
        }
    }
    Ok(())
}

/// 旧形式 (0.29.0 より前) の container stack と ECR repository を消す
pub async fn destroy_legacy_containers(
    ctx: &Context,
    stacks: &impl Stacks,
    repos: &impl Repositories,
    wait: WaitOptions,
    sink: &mut dyn Sink,
) -> Result<()> {
    let Some(general) = ctx.general.as_ref() else {
        return Ok(());
    };
    let region = &general.region;
    if let Some(status) = migrations::legacy_container_stack_status(ctx, stacks).await? {
        let name = migrations::legacy_container_stack_name(ctx);
        sink.log(&format!("Destroying legacy container stack '{name}'..."));
        if status != "DELETE_IN_PROGRESS" {
            stacks.delete(region, &name).await?;
        }
        let target = ResourceStatus::NoExist;
        stacks::wait(stacks, region, &name, "container", target, wait).await?;
        sink.success(&format!("Legacy container stack '{name}' was destroyed."));
    }
    if migrations::legacy_ecr_repo_exists(ctx, repos).await? {
        let repo = migrations::legacy_ecr_repo_name(ctx)?;
        sink.log(&format!("Destroying legacy ECR repository '{repo}'..."));
        repos.delete(region, &repo).await?;
        sink.success("Legacy ECR repository was deleted.");
    }
    Ok(())
}

/// recovery point (backup のデータ) の一覧と削除
pub trait RecoveryPoints {
    /// pocket が管理する vault にある recovery point の ARN。権限が無ければ `None`
    async fn list(&self) -> Result<Option<Vec<String>>>;
    async fn delete(&self, arn: &str) -> Result<()>;
}

/// destroy のときの recovery point の扱い。
///
/// 既定では消さない。`deletable = true` を宣言していて、対話で明示的に yes と答えたとき
/// だけ消す (`-y` の一括承認では消さない。データの削除だけは暗黙に通さない)
pub async fn destroy_recovery_points(
    points: &impl RecoveryPoints,
    declared: bool,
    deletable: bool,
    prompt: &impl Prompt,
    sink: &mut dyn Sink,
) -> Result<()> {
    let Some(arns) = points.list().await?.filter(|arns| !arns.is_empty()) else {
        return Ok(());
    };
    if declared && deletable {
        let count = arns.len();
        // `-y` のときは聞かない (データの削除は一括承認に含めない)
        if prompt.asks_explicitly() {
            sink.warning(&format!(
                "バックアップデータ (recovery point) が {count} 件あります。"
            ));
        }
        if prompt.confirm_explicit("recovery point も削除しますか？")? {
            for arn in &arns {
                points.delete(arn).await?;
            }
            sink.log(&format!("Deleted {count} recovery point(s)."));
            return Ok(());
        }
    }
    sink.warning(&format!(
        "バックアップデータ (recovery point {} 件) は削除されず残ります \
         (保持期限まで課金対象)。削除するには [backup] deletable = true を \
         宣言して `pocket backup cleanup` を実行するか、AWS Backup の\
         コンソールから削除してください。",
        arns.len()
    ));
    Ok(())
}

/// `provisioning = "command"` の DB は destroy でも触れない、という案内
pub fn command_provisioned_warning(label: &str, hint: &str) -> String {
    format!(
        "{label} は provisioning=\"command\" のため destroy では削除しません。\
         {hint} で削除してください。"
    )
}

/// provider を destroy が扱うか。宣言が無ければ `None`、command なら案内の文言
pub fn provider_action(
    provisioning: Option<ProvisioningMode>,
    label: &str,
    hint: &str,
) -> Option<Result<(), String>> {
    match provisioning? {
        ProvisioningMode::Command => Some(Err(command_provisioned_warning(label, hint))),
        ProvisioningMode::Deploy => Some(Ok(())),
    }
}

/// `[neon]` の宣言があるのに API key が無いときの案内
pub const NEON_CREDENTIAL_MISSING: &str =
    "Neon: NEON_API_KEY が未設定のため Neon の確認・削除をスキップします \
     (stack の削除に Neon の資格情報は不要です。Neon branch は \
     pocket resource neon delete か Neon コンソールで削除してください)。";

#[cfg(test)]
mod tests {
    use std::cell::RefCell;

    use super::*;
    use crate::deploy::interaction::testing::FakePrompt;
    use crate::deploy::migrations::testing::FakeRepositories;
    use crate::deploy::poll::testing::fast;
    use crate::deploy::stacks::fake::FakeStacks;
    use crate::stack::testing::{context_from_toml, vector_context};
    use crate::status::sink::{Recorded, Style};

    const ALL: DestroyOptions = DestroyOptions {
        with_secrets: true,
        with_state_bucket: false,
    };

    fn texts(sink: &Recorded, style: Style) -> Vec<&str> {
        sink.0
            .iter()
            .filter(|(_, s, _)| *s == style)
            .map(|(_, _, text)| text.as_str())
            .collect()
    }

    #[test]
    fn test_targets_follow_python_order_and_wording() {
        let ctx = vector_context("example/example-dsql.sandbox");
        let container = ctx.container.keys().next().unwrap().clone();
        let cf_name = ctx.cloudfront.keys().next().unwrap().clone();
        let facts = Facts {
            containers: vec![ContainerFact {
                name: container.clone(),
                ecr_exists: true,
                ecr_name_overridden: false,
            }],
            codebuild: true,
            backup_plans: vec!["plan-a".to_string()],
            dsql: true,
            s3: true,
            ..Facts::default()
        };
        let list = targets(&ctx, &facts, ALL);
        let inlet = ctx.inbound.keys().next().unwrap();
        let domain = ctx.inbound_domain.keys().next().unwrap();
        let mut want = vec![
            format!("CloudFront '{cf_name}' (CFNスタック + バケットポリシー)"),
            format!("CloudFront ACM '{cf_name}' (us-east-1 証明書)"),
            format!("Inbound {inlet} (原本バケットと配送DLQは保持)"),
            format!("Inbound domain {domain} (SES identity / DKIM / MX)"),
            format!("Container '{container}' (CFNスタック + ECR)"),
            "Container 共有リソース (secrets + CodeBuild)".to_string(),
            "AWS Backup plan: plan-a (recovery point と vault は残ります)".to_string(),
            format!("DSQL クラスター: {}", ctx.dsql.as_ref().unwrap().tag_name),
        ];
        want.push(format!(
            "S3 バケット: {}",
            ctx.s3.as_ref().unwrap().bucket_name
        ));
        assert_eq!(list, want);

        // --without-secrets と --with-state-bucket
        let options = DestroyOptions {
            with_secrets: false,
            with_state_bucket: true,
        };
        let list = targets(&ctx, &facts, options);
        assert!(list.contains(&"Container 共有リソース (CodeBuild)".to_string()));
        assert_eq!(list.last().unwrap(), "ステートバケット");
    }

    #[test]
    fn test_targets_for_optional_parts() {
        let ctx = vector_context("example/example-dsql.sandbox");
        let container = ctx.container.keys().next().unwrap().clone();
        let facts = Facts {
            containers: vec![ContainerFact {
                name: container.clone(),
                ecr_exists: true,
                ecr_name_overridden: true,
            }],
            legacy_stack: true,
            legacy_ecr: true,
            vpcs: vec![
                VpcFact {
                    manage: false,
                    ..VpcFact::default()
                },
                VpcFact {
                    manage: true,
                    stack_exists: true,
                    consumers: vec!["dev-other".to_string()],
                    efs_exists: true,
                },
                // stack も EFS も無い managed VPC は載せない
                VpcFact {
                    manage: true,
                    ..VpcFact::default()
                },
            ],
            ..Facts::default()
        };
        let list = targets(&ctx, &facts, ALL);
        assert!(list.contains(&format!(
            "Container '{container}' (CFNスタック + ECR は ecr_name 明示指定のため削除対象外)"
        )));
        assert!(list
            .iter()
            .any(|t| t.starts_with("旧形式の container stack '")
                && t.ends_with("(0.29.0 以前の [awscontainer] 命名。CFNスタック)")));
        assert!(list
            .iter()
            .any(|t| t.starts_with("旧形式の ECR repository '")));
        let vpcs: Vec<&String> = list.iter().filter(|t| t.starts_with("VPC")).collect();
        assert_eq!(
            vpcs,
            [
                "VPC (外部 VPC consumer タグ削除)",
                "VPC (CFNスタック + consumers: dev-other + EFS)"
            ]
        );
        // 在ると分かったものだけを載せる: DSQL / S3 が無ければ出さない
        assert!(!list.iter().any(|t| t.starts_with("DSQL")));
        assert!(!list.iter().any(|t| t.starts_with("S3")));
    }

    #[test]
    fn test_neon_target_depends_on_destroy_plan() {
        let toml = "[general]\nregion = \"ap-southeast-1\"\nproject_name = \"p\"\n\
                    stages = [\"prod\"]\n\n[neon]\nproject_name = \"np\"\n";
        let ctx = context_from_toml(toml, "prod");
        let with = |plan| {
            let facts = Facts {
                neon: plan,
                ..Facts::default()
            };
            targets(&ctx, &facts, ALL)
        };
        assert_eq!(with(Some(DestroyPlan::Branch)), ["Neon ブランチ"]);
        assert_eq!(
            with(Some(DestroyPlan::Project)),
            ["Neon プロジェクト 'np' (root branch のため project ごと削除)"]
        );
        // 消せない root branch と、branch が無い場合は一覧に載せない
        assert!(with(Some(DestroyPlan::Blocked)).is_empty());
        assert!(with(None).is_empty());
    }

    #[test]
    fn test_steps_are_reverse_of_deploy() {
        let all = steps(DestroyOptions {
            with_secrets: true,
            with_state_bucket: true,
        });
        assert_eq!(
            all,
            [
                Step::Inbound,
                Step::InboundDomain,
                Step::CloudFrontAndAcm,
                Step::Containers,
                Step::LegacyContainers,
                Step::CodeBuild,
                Step::LogGroups,
                Step::Secrets,
                Step::Backup,
                Step::Dsql,
                Step::Rds,
                Step::Vpc,
                Step::CloudFrontKeys,
                Step::S3,
                Step::Tidb,
                Step::Upstash,
                Step::Neon,
                Step::StateBucket,
            ]
        );
        let kept = steps(DestroyOptions {
            with_secrets: false,
            with_state_bucket: false,
        });
        assert!(!kept.contains(&Step::Secrets));
        assert!(!kept.contains(&Step::StateBucket));
        assert_eq!(kept.len(), all.len() - 2);
    }

    struct FakeOps {
        targets: Vec<String>,
        steps: Vec<Step>,
        fail_on: Option<Step>,
    }

    impl DestroyOps for FakeOps {
        async fn targets(
            &mut self,
            _options: DestroyOptions,
            _sink: &mut dyn Sink,
        ) -> Result<Vec<String>> {
            Ok(self.targets.clone())
        }

        async fn step(&mut self, step: Step, _sink: &mut dyn Sink) -> Result<()> {
            if self.fail_on == Some(step) {
                bail!("{step:?} failed");
            }
            self.steps.push(step);
            Ok(())
        }
    }

    fn ops(targets: &[&str]) -> FakeOps {
        FakeOps {
            targets: targets.iter().map(|t| t.to_string()).collect(),
            steps: Vec::new(),
            fail_on: None,
        }
    }

    #[tokio::test]
    async fn test_run_lists_targets_and_asks_before_deleting() {
        let mut fake = ops(&["S3 バケット: b", "ステートバケット"]);
        let prompt = FakePrompt::answering(true);
        let mut sink = Recorded::default();
        run(&mut fake, "prod", ALL, &prompt, &mut sink)
            .await
            .unwrap();
        assert_eq!(
            texts(&sink, Style::Danger),
            ["以下のリソースを削除します:", "この操作は取り消せません！"]
        );
        assert_eq!(
            texts(&sink, Style::Info),
            ["  - S3 バケット: b", "  - ステートバケット"]
        );
        assert_eq!(
            prompt.asked.borrow().as_slice(),
            ["stage 'prod' の全リソースを削除しますか？"]
        );
        assert_eq!(fake.steps, steps(ALL));
        assert_eq!(
            texts(&sink, Style::Success),
            ["stage 'prod' の全リソースを削除しました。"]
        );
    }

    #[tokio::test]
    async fn test_run_deletes_nothing_when_declined_or_nothing_to_delete() {
        let mut fake = ops(&["S3 バケット: b"]);
        let prompt = FakePrompt::answering(false);
        let error = run(&mut fake, "prod", ALL, &prompt, &mut Recorded::default())
            .await
            .unwrap_err();
        assert_eq!(error.to_string(), "Aborted!");
        assert!(fake.steps.is_empty());

        // 対象が無ければ聞かずに終わる
        let mut fake = ops(&[]);
        let prompt = FakePrompt::answering(true);
        let mut sink = Recorded::default();
        run(&mut fake, "prod", ALL, &prompt, &mut sink)
            .await
            .unwrap();
        assert!(prompt.asked.borrow().is_empty());
        assert!(fake.steps.is_empty());
        assert_eq!(
            texts(&sink, Style::Warning),
            ["削除対象のリソースが見つかりません。"]
        );
    }

    #[tokio::test]
    async fn test_run_with_yes_skips_the_question_and_stops_at_failure() {
        let mut fake = ops(&["RDS Aurora クラスター: c"]);
        fake.fail_on = Some(Step::Rds);
        let prompt = FakePrompt::yes();
        let mut sink = Recorded::default();
        let error = run(&mut fake, "prod", ALL, &prompt, &mut sink)
            .await
            .unwrap_err();
        assert_eq!(error.to_string(), "Rds failed");
        assert!(prompt.asked.borrow().is_empty());
        // 失敗した手順より後 (VPC など) には進まず、成功の表示も出さない
        assert_eq!(fake.steps.last(), Some(&Step::Dsql));
        assert!(texts(&sink, Style::Success).is_empty());
    }

    fn container() -> ContainerContext {
        let ctx = vector_context("example/example-dsql.sandbox");
        let container = ctx.container.values().next().unwrap().clone();
        container
    }

    #[tokio::test]
    async fn test_destroy_container_deletes_stack_then_repository() {
        let c = container();
        let name = container_stack::stack_name(&c);
        let stacks = FakeStacks::default();
        stacks.put(&name, FakeStacks::description("UPDATE_COMPLETE", "h", &[]));
        let repos = FakeRepositories::with(&[c.ecr_name.as_str()]);
        let mut sink = Recorded::default();
        destroy_container(&c, &stacks, &repos, fast(3), &mut sink)
            .await
            .unwrap();
        assert_eq!(stacks.writes(), [format!("delete {} {name}", c.region)]);
        assert_eq!(
            repos.deleted.borrow().as_slice(),
            std::slice::from_ref(&c.ecr_name)
        );
        assert_eq!(
            texts(&sink, Style::Success),
            [
                format!("Container '{}' stack was destroyed.", c.name).as_str(),
                "ECR repository was deleted."
            ]
        );
    }

    #[tokio::test]
    async fn test_destroy_container_resumes_after_interruption() {
        let c = container();
        let name = container_stack::stack_name(&c);
        // 前回の destroy が stack の削除を出した後に止まった: もう一度 delete を出さずに待つ
        let stacks = FakeStacks::default();
        stacks.put(
            &name,
            FakeStacks::description("DELETE_IN_PROGRESS", "h", &[]),
        );
        let repos = FakeRepositories::with(&[c.ecr_name.as_str()]);
        let error = destroy_container(&c, &stacks, &repos, fast(2), &mut Recorded::default())
            .await
            .unwrap_err();
        assert!(error.to_string().contains("タイムアウト"), "{error}");
        assert!(stacks.writes().is_empty());
        // 待っている間は repository を消さない
        assert!(repos.deleted.borrow().is_empty());

        // stack の削除は終わっていて、repository だけが残っている: 残りを消す
        let stacks = FakeStacks::default();
        let mut sink = Recorded::default();
        destroy_container(&c, &stacks, &repos, fast(2), &mut sink)
            .await
            .unwrap();
        assert!(stacks.writes().is_empty());
        assert_eq!(
            repos.deleted.borrow().as_slice(),
            std::slice::from_ref(&c.ecr_name)
        );

        // 全部消えた後の再実行は何もしない
        let mut sink = Recorded::default();
        destroy_container(&c, &stacks, &repos, fast(2), &mut sink)
            .await
            .unwrap();
        assert!(sink.0.is_empty());
        assert_eq!(repos.deleted.borrow().len(), 1);
    }

    #[tokio::test]
    async fn test_destroy_container_keeps_explicitly_named_repository() {
        let mut c = container();
        c.ecr_name_overridden = true;
        let stacks = FakeStacks::default();
        let repos = FakeRepositories::with(&[c.ecr_name.as_str()]);
        let mut sink = Recorded::default();
        destroy_container(&c, &stacks, &repos, fast(2), &mut sink)
            .await
            .unwrap();
        assert!(repos.deleted.borrow().is_empty());
        assert!(texts(&sink, Style::Warning)[0].contains("ecr_name で明示指定されているため削除"));
    }

    #[tokio::test]
    async fn test_destroy_legacy_containers() {
        let ctx = vector_context("example/example-dsql.sandbox");
        let stack = migrations::legacy_container_stack_name(&ctx);
        let repo = migrations::legacy_ecr_repo_name(&ctx).unwrap();
        let stacks = FakeStacks::default();
        stacks.put(&stack, FakeStacks::description("UPDATE_COMPLETE", "h", &[]));
        let repos = FakeRepositories::with(&[repo.as_str()]);
        let mut sink = Recorded::default();
        destroy_legacy_containers(&ctx, &stacks, &repos, fast(3), &mut sink)
            .await
            .unwrap();
        assert_eq!(stacks.writes().len(), 1);
        assert_eq!(repos.deleted.borrow().as_slice(), [repo]);
        // 旧形式が無い環境では何もしない
        let mut sink = Recorded::default();
        destroy_legacy_containers(&ctx, &stacks, &repos, fast(3), &mut sink)
            .await
            .unwrap();
        assert!(sink.0.is_empty());
        assert_eq!(stacks.writes().len(), 1);
    }

    struct FakePoints {
        arns: Option<Vec<String>>,
        deleted: RefCell<Vec<String>>,
    }

    impl FakePoints {
        fn with(count: usize) -> Self {
            Self {
                arns: Some((0..count).map(|i| format!("arn:rp:{i}")).collect()),
                deleted: RefCell::default(),
            }
        }
    }

    impl RecoveryPoints for FakePoints {
        async fn list(&self) -> Result<Option<Vec<String>>> {
            Ok(self.arns.clone())
        }

        async fn delete(&self, arn: &str) -> Result<()> {
            self.deleted.borrow_mut().push(arn.to_string());
            Ok(())
        }
    }

    #[tokio::test]
    async fn test_recovery_points_are_never_deleted_by_assume_yes() {
        let points = FakePoints::with(2);
        let prompt = FakePrompt::yes();
        let mut sink = Recorded::default();
        destroy_recovery_points(&points, true, true, &prompt, &mut sink)
            .await
            .unwrap();
        assert!(points.deleted.borrow().is_empty());
        assert!(prompt.asked.borrow().is_empty());
        let warnings = texts(&sink, Style::Warning);
        assert_eq!(warnings.len(), 1);
        assert!(warnings[0]
            .starts_with("バックアップデータ (recovery point 2 件) は削除されず残ります"));
    }

    #[tokio::test]
    async fn test_recovery_points_need_declaration_and_explicit_yes() {
        // 対話で yes と答えたときだけ消す
        let points = FakePoints::with(2);
        let prompt = FakePrompt::answering(true);
        let mut sink = Recorded::default();
        destroy_recovery_points(&points, true, true, &prompt, &mut sink)
            .await
            .unwrap();
        assert_eq!(points.deleted.borrow().len(), 2);
        assert_eq!(
            prompt.asked.borrow().as_slice(),
            ["recovery point も削除しますか？"]
        );
        assert_eq!(
            texts(&sink, Style::Warning),
            ["バックアップデータ (recovery point) が 2 件あります。"]
        );
        // No と答えた・deletable の宣言が無い・backup の宣言が無い → 消さずに残ることを知らせる
        for (declared, deletable, answer) in [
            (true, true, false),
            (true, false, true),
            (false, true, true),
        ] {
            let points = FakePoints::with(1);
            let prompt = FakePrompt::answering(answer);
            let mut sink = Recorded::default();
            destroy_recovery_points(&points, declared, deletable, &prompt, &mut sink)
                .await
                .unwrap();
            assert!(points.deleted.borrow().is_empty());
            assert!(texts(&sink, Style::Warning)
                .last()
                .unwrap()
                .contains("は削除されず残ります"));
        }
    }

    #[tokio::test]
    async fn test_recovery_points_absent_or_unreadable() {
        for points in [
            FakePoints::with(0),
            FakePoints {
                arns: None,
                deleted: RefCell::default(),
            },
        ] {
            let prompt = FakePrompt::answering(true);
            let mut sink = Recorded::default();
            destroy_recovery_points(&points, true, true, &prompt, &mut sink)
                .await
                .unwrap();
            assert!(sink.0.is_empty());
            assert!(prompt.asked.borrow().is_empty());
        }
    }

    #[test]
    fn test_provider_action() {
        assert_eq!(provider_action(None, "TiDB", "x"), None);
        assert_eq!(
            provider_action(Some(ProvisioningMode::Deploy), "TiDB", "x"),
            Some(Ok(()))
        );
        assert_eq!(
            provider_action(
                Some(ProvisioningMode::Command),
                "TiDB",
                "pocket resource tidb delete"
            ),
            Some(Err(
                "TiDB は provisioning=\"command\" のため destroy では削除しません。\
                 pocket resource tidb delete で削除してください。"
                    .to_string()
            ))
        );
    }
}
