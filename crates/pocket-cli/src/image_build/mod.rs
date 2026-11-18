//! `pocket-rs build`: 作業ツリーを build し、git commit hash を tag にして ECR へ push する。
//!
//! Python 版 `pocket build` (`build_cli.py` / `deploy_cli.build_image` / `Ecr` / builders)
//! の移植。deploy はしない (build once)。昇格 (`promote`) は deploy の Rust 化と一緒に行う。
//!
//! 流れは Python 版と同じ: 認証の確認 → dirty の検査 → tag (commit hash) の決定 →
//! Context → state bucket の確保 (CodeBuild の source 置き場) → container 名順に
//! ECR repository の確保と build / push。

pub(crate) mod codebuild;
mod context_files;
mod docker;
pub(crate) mod runner;

use std::path::PathBuf;
use std::time::{Duration, Instant};

use anyhow::{bail, Context as _, Result};
use aws_config::SdkConfig;
use aws_sdk_s3::error::ProvideErrorMetadata;
use aws_sdk_s3::types::{
    BucketLocationConstraint, CreateBucketConfiguration, PublicAccessBlockConfiguration,
};
use magic_pocket_settings::context::{ContainerContext, Context};
use magic_pocket_settings::model::container::BuildBackend;

use self::codebuild::{resolve_permissions_boundary, Clients, CodeBuildBuilder, CodeBuildSettings};
use self::runner::{Cmd, Runner, SystemRunner};

/// builder に渡す 1 container 分の build
#[derive(Debug, Clone)]
pub struct BuildRequest {
    /// `<repository uri>:<tag>`
    pub target: String,
    /// build context (= pocket.toml のディレクトリ、絶対パス)
    pub context_dir: PathBuf,
    /// context_dir からの相対パス
    pub dockerfile_path: String,
    pub platform: String,
    pub region: String,
}

/// 削除直後の同名 bucket は削除の伝播が終わるまで CreateBucket が OperationAborted で
/// 拒否される。待って再試行する (Python 版 `s3_utils.create_bucket`)
const CREATE_BUCKET_RETRY_INTERVAL: Duration = Duration::from_secs(30);
const CREATE_BUCKET_RETRY_TIMEOUT: Duration = Duration::from_secs(3600);

pub async fn run_build(stage: &str, allow_dirty: bool) -> Result<()> {
    let runner = SystemRunner;
    let sdk = aws_config::defaults(aws_config::BehaviorVersion::latest())
        .load()
        .await;
    let account_id = check_aws_credentials(&sdk).await?;
    if !allow_dirty && is_working_tree_dirty(&runner) {
        bail!(
            "working tree に未コミットの変更があります。build once では \
             commit hash と image 内容の一致が前提のため、commit してから \
             build してください (--allow-dirty で回避できますが、その image の \
             昇格は推奨しません)。"
        );
    }
    let env_hash = std::env::var("COMMIT_HASH").ok();
    let tag = get_commit_hash(env_hash.as_deref(), &runner)?;
    let (context, toml_dir) = crate::context::load_context(stage)?;
    let context_dir = std::fs::canonicalize(&toml_dir)
        .with_context(|| format!("{} を解決できません", toml_dir.display()))?;
    let targets = build_images(&sdk, &context, &context_dir, &tag, &account_id, &runner).await?;
    for target in targets {
        eprintln!("built and pushed: {target}");
    }
    Ok(())
}

/// 全 container の image を tag で build & push する。戻り値は `ecr_name:tag` (container 名順)
async fn build_images(
    sdk: &SdkConfig,
    context: &Context,
    context_dir: &std::path::Path,
    tag: &str,
    account_id: &str,
    runner: &dyn Runner,
) -> Result<Vec<String>> {
    if context.container.is_empty() {
        bail!("container がこの stage に設定されていません。");
    }
    let general = context
        .general
        .as_ref()
        .context("general context is not configured")?;
    let state_bucket = format!("{}state", resource_prefix(context, general));
    ensure_state_bucket(
        &s3_client(sdk, &general.region),
        &state_bucket,
        &general.region,
    )
    .await?;

    let mut containers: Vec<(&String, &ContainerContext)> = context.container.iter().collect();
    containers.sort_by_key(|(name, _)| *name);
    let mut targets = Vec::new();
    for (name, c) in containers {
        eprintln!("Building container '{name}'...");
        build_container_image(
            sdk,
            c,
            context_dir,
            tag,
            account_id,
            &state_bucket,
            &general.region,
            runner,
        )
        .await?;
        targets.push(format!("{}:{tag}", c.ecr_name));
    }
    Ok(targets)
}

/// container 1 つの image を `tag` で build & push する (repository が無ければ作る)。
///
/// state bucket (CodeBuild backend の source 置き場) は呼び出し側が用意しておく
#[allow(clippy::too_many_arguments)]
pub(crate) async fn build_container_image(
    sdk: &SdkConfig,
    c: &ContainerContext,
    context_dir: &std::path::Path,
    tag: &str,
    account_id: &str,
    state_bucket: &str,
    state_bucket_region: &str,
    runner: &dyn Runner,
) -> Result<()> {
    let region_config = regional_config(sdk, &c.region);
    let ecr = aws_sdk_ecr::Client::new(&region_config);
    let uri = ensure_repository(&ecr, &c.ecr_name).await?;
    let req = BuildRequest {
        target: format!("{uri}:{tag}"),
        context_dir: context_dir.to_path_buf(),
        dockerfile_path: c.dockerfile_path.clone(),
        platform: platform_str(c),
        region: c.region.clone(),
    };
    match c.build.backend {
        BuildBackend::Docker => docker::docker_build_and_push(&req, &ecr, runner).await?,
        BuildBackend::Depot => {
            docker::depot_build_and_push(&req, c.build.depot_project_id.as_deref(), &ecr, runner)
                .await?
        }
        BuildBackend::Codebuild => {
            let settings = CodeBuildSettings {
                resource_prefix: c.resource_prefix.clone(),
                state_bucket: state_bucket.to_string(),
                compute_type: c.build.compute_type.clone(),
                permissions_boundary: resolve_permissions_boundary(
                    std::env::var("CODEBUILD_PERMISSIONS_BOUNDARY").ok(),
                    c.permissions_boundary.as_deref(),
                ),
                external_roles: c.external_roles,
                external_role_prefix: c.external_role_prefix.clone(),
            };
            let clients = Clients {
                codebuild: aws_sdk_codebuild::Client::new(&region_config),
                iam: aws_sdk_iam::Client::new(&region_config),
                s3: s3_client(sdk, state_bucket_region),
            };
            CodeBuildBuilder::new(&settings, &clients)
                .build_and_push(&req, account_id)
                .await?
        }
    }
    Ok(())
}

/// Python 版 `context_resource_prefix` (state bucket 名の prefix)
fn resource_prefix(
    context: &Context,
    general: &magic_pocket_settings::context::GeneralContext,
) -> String {
    general
        .prefix_template
        .replace("{stage}", &context.stage)
        .replace("{project}", &context.project_name)
        .replace("{namespace}", &general.namespace)
}

fn platform_str(c: &ContainerContext) -> String {
    serde_json::to_value(c.platform)
        .ok()
        .and_then(|v| v.as_str().map(str::to_string))
        .expect("Platform は文字列に serialize される")
}

fn regional_config(sdk: &SdkConfig, region: &str) -> SdkConfig {
    sdk.to_builder()
        .region(aws_config::Region::new(region.to_string()))
        .build()
}

fn s3_client(sdk: &SdkConfig, region: &str) -> aws_sdk_s3::Client {
    let config = regional_config(sdk, region);
    // moto 等のカスタム endpoint では path-style で呼ぶ (static 系と同じ)
    let custom_endpoint = ["AWS_ENDPOINT_URL_S3", "AWS_ENDPOINT_URL"]
        .iter()
        .any(|k| std::env::var(k).map(|v| !v.is_empty()).unwrap_or(false));
    let builder = aws_sdk_s3::config::Builder::from(&config).force_path_style(custom_endpoint);
    aws_sdk_s3::Client::from_conf(builder.build())
}

/// AWS の認証情報を確かめ、account id を返す (Python 版 `check_aws_credentials`)
pub(crate) async fn check_aws_credentials(sdk: &SdkConfig) -> Result<String> {
    // region 未設定でも STS は呼べるよう、無ければ us-east-1 で呼ぶ
    let config = if sdk.region().is_some() {
        sdk.clone()
    } else {
        regional_config(sdk, "us-east-1")
    };
    let sts = aws_sdk_sts::Client::new(&config);
    match sts.get_caller_identity().send().await {
        Ok(res) => res
            .account()
            .map(str::to_string)
            .context("STS get_caller_identity が account を返しませんでした"),
        Err(e) => {
            let expired = e
                .as_service_error()
                .and_then(|s| s.code())
                .is_some_and(|c| matches!(c, "ExpiredToken" | "ExpiredTokenException"));
            let message = if expired {
                "AWS 認証トークンの有効期限が切れています。".to_string()
            } else if e.as_service_error().is_some() {
                return Err(e).context("STS get_caller_identity に失敗しました");
            } else {
                format!("AWS 認証情報を確認できません ({e})。")
            };
            print_auth_guide(&message);
            std::process::exit(1);
        }
    }
}

/// `COMMIT_HASH` があればそれを、無ければ `git rev-parse HEAD`。取得できなければエラー
/// (誤った tag で push しないよう、deploy_hash のように "unknown" へは落とさない)
fn get_commit_hash(env_hash: Option<&str>, runner: &dyn Runner) -> Result<String> {
    if let Some(hash) = env_hash.filter(|h| !h.is_empty()) {
        return Ok(hash.to_string());
    }
    let failure = "git commit hash の取得に失敗しました (git リポジトリ外?)。 \
                   COMMIT_HASH 環境変数で明示指定もできます。";
    match runner.capture(&Cmd::new("git", ["rev-parse", "HEAD"])) {
        Ok(out) if out.success => Ok(out.stdout.trim().to_string()),
        _ => bail!(failure),
    }
}

/// 未コミットの変更があるか。判定できない (git リポジトリ外等) ときは dirty 扱いしない
fn is_working_tree_dirty(runner: &dyn Runner) -> bool {
    match runner.capture(&Cmd::new("git", ["status", "--porcelain"])) {
        Ok(out) if out.success => !out.stdout.trim().is_empty(),
        _ => false,
    }
}

/// ECR repository を名前で引き、無ければ作る。repository の URI を返す
async fn ensure_repository(ecr: &aws_sdk_ecr::Client, name: &str) -> Result<String> {
    // 全列挙は既定 100 件で切れるため名前指定で引く
    match ecr
        .describe_repositories()
        .repository_names(name)
        .send()
        .await
    {
        Ok(res) => {
            if let Some(uri) = res.repositories().first().and_then(|r| r.repository_uri()) {
                return Ok(uri.to_string());
            }
        }
        Err(e) => {
            let not_found = e
                .as_service_error()
                .is_some_and(|s| s.is_repository_not_found_exception());
            if !not_found {
                return Err(e)
                    .with_context(|| format!("ECR describe_repositories ({name}) に失敗しました"));
            }
        }
    }
    println!("Creating repository ...");
    println!("  {name}");
    let res = ecr
        .create_repository()
        .repository_name(name)
        .send()
        .await
        .with_context(|| format!("ECR create_repository ({name}) に失敗しました"))?;
    let uri = res
        .repository()
        .and_then(|r| r.repository_uri())
        .context("ECR create_repository が URI を返しませんでした")?
        .to_string();
    println!("  {uri}");
    Ok(uri)
}

/// state bucket の region のずれを案内する docs (Python 版と同じ)
const STATE_BUCKET_REGION_DOCS: &str =
    "https://worgue.github.io/magic-pocket/guide/configuration/#state-bucket-region";

/// state bucket が `[general] region` と別の region にあるときのエラー (Python 版
/// `StateBucketRegionError` と同じ文言)
fn state_bucket_region_error(bucket: &str, actual: &str, expected: &str) -> anyhow::Error {
    anyhow::anyhow!(
        "state bucket {bucket} は {actual} にありますが、[general] region は {expected} です。\
         region を移したときに state bucket だけ旧 region に残っています。\
         state bucket を {expected} へ移してから再実行してください (手順: {STATE_BUCKET_REGION_DOCS})。"
    )
}

/// state bucket が無ければ作る (全公開ブロック)。
///
/// 既存の bucket が `region` 以外にあればエラーにする。HeadBucket は別 region の bucket に
/// 301 を返す (boto3 は自動で追うので Python 版では黙って吸収されていた)。吸収すると
/// region 移設の取り残しに気付けず、CodeBuild (source bucket が project と同じ region に
/// あることを求める) で分かりにくく落ちるため止める
async fn ensure_state_bucket(s3: &aws_sdk_s3::Client, bucket: &str, region: &str) -> Result<()> {
    match s3.head_bucket().bucket(bucket).send().await {
        Ok(res) => {
            return match res.bucket_region() {
                Some(actual) if actual != region => {
                    Err(state_bucket_region_error(bucket, actual, region))
                }
                _ => Ok(()),
            };
        }
        Err(e) => {
            let raw = e.raw_response();
            let status = raw.map(|r| r.status().as_u16());
            if status == Some(301) {
                let actual = raw
                    .and_then(|r| r.headers().get("x-amz-bucket-region"))
                    .unwrap_or("別の region");
                return Err(state_bucket_region_error(bucket, actual, region));
            }
            if status != Some(404) {
                return Err(e).with_context(|| format!("S3 head_bucket ({bucket}) に失敗しました"));
            }
        }
    }
    let deadline = Instant::now() + CREATE_BUCKET_RETRY_TIMEOUT;
    loop {
        let mut create = s3.create_bucket().bucket(bucket);
        if region != "us-east-1" {
            create = create.create_bucket_configuration(
                CreateBucketConfiguration::builder()
                    .location_constraint(BucketLocationConstraint::from(region))
                    .build(),
            );
        }
        match create.send().await {
            Ok(_) => break,
            Err(e) => {
                if e.as_service_error().and_then(|s| s.code()) != Some("OperationAborted") {
                    return Err(e)
                        .with_context(|| format!("S3 create_bucket ({bucket}) に失敗しました"));
                }
                let now = Instant::now();
                if now >= deadline {
                    bail!(
                        "bucket '{bucket}' の作成が OperationAborted のまま {} 分以内に成功\
                         しませんでした。削除直後の同名 bucket は削除の伝播 (最大 1 時間\
                         程度) が終わるまで再作成できません。時間を置いて再実行して\
                         ください。",
                        CREATE_BUCKET_RETRY_TIMEOUT.as_secs() / 60
                    );
                }
                eprintln!(
                    "bucket '{bucket}' の作成が OperationAborted で拒否されました (削除直後の\
                     同名 bucket は削除の伝播が終わるまで再作成できません)。{} 秒後に\
                     再試行します (最長あと {} 分待ちます)...",
                    CREATE_BUCKET_RETRY_INTERVAL.as_secs(),
                    (deadline - now).as_secs() / 60
                );
                tokio::time::sleep(CREATE_BUCKET_RETRY_INTERVAL).await;
            }
        }
    }
    s3.put_public_access_block()
        .bucket(bucket)
        .public_access_block_configuration(
            PublicAccessBlockConfiguration::builder()
                .block_public_acls(true)
                .ignore_public_acls(true)
                .block_public_policy(true)
                .restrict_public_buckets(true)
                .build(),
        )
        .send()
        .await
        .with_context(|| format!("S3 put_public_access_block ({bucket}) に失敗しました"))?;
    Ok(())
}

fn print_auth_guide(message: &str) {
    eprintln!(
        "{message}\n\
         \n\
         以下のいずれかで認証してください:\n\
         \n\
         \x20 SSO の場合:\n\
         \x20   aws sso login\n\
         \x20   aws sso login --profile <profile-name>\n\
         \n\
         \x20 IAM ユーザーの場合:\n\
         \x20   aws configure\n\
         \n\
         \x20 環境変数の場合:\n\
         \x20   export AWS_ACCESS_KEY_ID=...\n\
         \x20   export AWS_SECRET_ACCESS_KEY=..."
    );
}

#[cfg(test)]
mod tests {
    use super::runner::fake::FakeRunner;
    use super::*;

    #[test]
    fn region_error_matches_python() {
        let message =
            state_bucket_region_error("b-state", "ap-northeast-1", "ap-southeast-1").to_string();
        assert_eq!(
            message,
            "state bucket b-state は ap-northeast-1 にありますが、[general] region は \
             ap-southeast-1 です。region を移したときに state bucket だけ旧 region に残って\
             います。state bucket を ap-southeast-1 へ移してから再実行してください (手順: \
             https://worgue.github.io/magic-pocket/guide/configuration/#state-bucket-region)。"
        );
    }

    #[test]
    fn commit_hash_prefers_env() {
        let runner = FakeRunner::default().respond("git", "rev-parse", true, "abc123\n");
        assert_eq!(get_commit_hash(Some("envsha"), &runner).unwrap(), "envsha");
        assert!(runner.calls.borrow().is_empty());
        assert_eq!(get_commit_hash(Some(""), &runner).unwrap(), "abc123");
        assert_eq!(get_commit_hash(None, &runner).unwrap(), "abc123");
        assert_eq!(
            runner.calls.borrow()[0],
            Cmd::new("git", ["rev-parse", "HEAD"])
        );
    }

    #[test]
    fn commit_hash_failure_is_error() {
        let failed = FakeRunner::default().respond("git", "rev-parse", false, "");
        let err = get_commit_hash(None, &failed).unwrap_err().to_string();
        assert!(err.starts_with("git commit hash の取得に失敗しました"));
        // git が無い (起動できない) ときも同じ
        assert!(get_commit_hash(None, &FakeRunner::default()).is_err());
    }

    #[test]
    fn dirty_detection() {
        let dirty = FakeRunner::default().respond("git", "status", true, " M a.py\n");
        assert!(is_working_tree_dirty(&dirty));
        let clean = FakeRunner::default().respond("git", "status", true, "\n");
        assert!(!is_working_tree_dirty(&clean));
        // 判定できないときは dirty 扱いしない
        let outside = FakeRunner::default().respond("git", "status", false, "");
        assert!(!is_working_tree_dirty(&outside));
        assert!(!is_working_tree_dirty(&FakeRunner::default()));
    }
}
