//! CodeBuild backend (source zip を state bucket に上げ、CodeBuild で build して push する)。
//!
//! Python 版 `builders/codebuild.py` と `iam_roles.py` の CodeBuild role 部分の移植。
//! project 名・role 名・source の key・buildspec・role の policy は Python 版と同じにする
//! (同じ stage を Python 版と Rust 版の両方から build できるように)。

use std::fs::File;
use std::io::{self, Seek, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::time::Duration;

use anyhow::{bail, Context as _, Result};
use aws_sdk_codebuild::types::{
    ArtifactsType, ComputeType, EnvironmentType, EnvironmentVariable, EnvironmentVariableType,
    ProjectArtifacts, ProjectEnvironment, ProjectSource, SourceType, StatusType,
};
use aws_sdk_s3::primitives::ByteStream;
use zip::write::SimpleFileOptions;
use zip::{CompressionMethod, ZipWriter};

use super::context_files::{iter_source_files, load_dockerignore};
use super::BuildRequest;
use crate::deploy::iam;
use crate::stack::roles::{codebuild_role, CodeBuildRole};

pub const IMAGE_AMD64: &str = "aws/codebuild/amazonlinux-x86_64-standard:5.0";
pub const IMAGE_ARM64: &str = "aws/codebuild/amazonlinux-aarch64-standard:3.0";

pub const BUILDSPEC: &str = "version: 0.2\n\
phases:\n  \
  pre_build:\n    \
    commands:\n      \
      - >-\n        \
        aws ecr get-login-password --region $AWS_DEFAULT_REGION\n        \
        | docker login --username AWS\n        \
        --password-stdin $ECR_HOST\n  \
  build:\n    \
    commands:\n      \
      - docker build -t $IMAGE_TAG -f $DOCKERFILE .\n  \
  post_build:\n    \
    commands:\n      \
      - docker push $IMAGE_TAG\n";

const POLL_INTERVAL: Duration = Duration::from_secs(10);

/// project / role / source key の名前 (Python 版 `CodeBuildBuilder.__init__`)
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Names {
    pub project: String,
    pub role: String,
    /// 0.36 以前の role 名。新 role へ付け替えた後に削除する (改名互換)
    pub legacy_role: String,
    pub source_key: String,
}

impl Names {
    pub fn new(resource_prefix: &str, external_roles: bool, external_role_prefix: &str) -> Self {
        let project = format!("{resource_prefix}codebuild");
        // external_roles では利用者が作る種別単位の role ({external_role_prefix}codebuild-role)
        let role = if external_roles && !external_role_prefix.is_empty() {
            format!("{external_role_prefix}codebuild-role")
        } else {
            format!("{resource_prefix}codebuild-role")
        };
        Self {
            legacy_role: format!("forge-{resource_prefix}codebuild-role"),
            source_key: format!("codebuild/{project}/source.zip"),
            project,
            role,
        }
    }
}

pub struct CodeBuildSettings {
    pub resource_prefix: String,
    pub state_bucket: String,
    pub compute_type: String,
    pub permissions_boundary: Option<String>,
    pub external_roles: bool,
    pub external_role_prefix: String,
}

pub struct Clients {
    pub codebuild: aws_sdk_codebuild::Client,
    pub iam: aws_sdk_iam::Client,
    pub s3: aws_sdk_s3::Client,
}

/// `CODEBUILD_PERMISSIONS_BOUNDARY` があればそれを、無ければ container の設定値
pub fn resolve_permissions_boundary(
    env: Option<String>,
    configured: Option<&str>,
) -> Option<String> {
    env.filter(|v| !v.is_empty())
        .or_else(|| configured.map(str::to_string))
}

pub fn env_for_platform(platform: &str) -> (EnvironmentType, &'static str) {
    if platform.contains("arm64") || platform.contains("aarch64") {
        (EnvironmentType::ArmContainer, IMAGE_ARM64)
    } else {
        (EnvironmentType::LinuxContainer, IMAGE_AMD64)
    }
}

/// 1 ファイルの zip 内 mode。実行ビットがあれば 0755、無ければ 0644 に正規化する
/// (forge VM では 0600 のファイルが生まれやすく、そのまま image に入ると Lambda の非 root
/// 実行ユーザーが読めず起動時に失敗するため)
pub fn normalized_mode(mode: u32) -> u32 {
    if mode & 0o100 != 0 {
        0o755
    } else {
        0o644
    }
}

/// build context を zip にして writer へ書く。戻り値は書いたファイル数。
/// 壊れた symlink は skip して警告する
pub fn write_source_zip<W: Write + Seek>(context_dir: &Path, writer: W) -> Result<usize> {
    let spec = load_dockerignore(context_dir)?;
    let mut zip = ZipWriter::new(writer);
    let mut count = 0;
    for file in iter_source_files(context_dir, &spec)? {
        if file.is_broken_symlink() {
            eprintln!("  壊れた symlink を skip: {}", file.rel);
            continue;
        }
        let mode = std::fs::metadata(&file.abs)?.permissions().mode();
        let options = SimpleFileOptions::default()
            .compression_method(CompressionMethod::Deflated)
            .unix_permissions(normalized_mode(mode));
        zip.start_file(file.rel.as_str(), options)?;
        let mut src = File::open(&file.abs)
            .with_context(|| format!("{} を読めません", file.abs.display()))?;
        io::copy(&mut src, &mut zip)?;
        count += 1;
    }
    zip.finish()?;
    Ok(count)
}

pub struct CodeBuildBuilder<'a> {
    pub settings: &'a CodeBuildSettings,
    pub clients: &'a Clients,
    pub names: Names,
}

impl<'a> CodeBuildBuilder<'a> {
    pub fn new(settings: &'a CodeBuildSettings, clients: &'a Clients) -> Self {
        let names = Names::new(
            &settings.resource_prefix,
            settings.external_roles,
            &settings.external_role_prefix,
        );
        Self {
            settings,
            clients,
            names,
        }
    }

    pub async fn build_and_push(&self, req: &BuildRequest, account_id: &str) -> Result<()> {
        println!("CodeBuild でイメージをビルドします...");
        println!("  context: {}", req.context_dir.display());
        println!("  target: {}", req.target);
        println!("  dockerfile: {}", req.dockerfile_path);
        println!("  platform: {}", req.platform);

        let ecr_host = format!("{account_id}.dkr.ecr.{}.amazonaws.com", req.region);
        let role_arn = self.ensure_role(&req.region, account_id).await?;
        self.ensure_project(&req.platform, &role_arn).await?;
        // project の serviceRole を新ロールへ付け替えた後なので、旧名ロールは孤児
        if !self.settings.external_roles {
            self.delete_role(&self.names.legacy_role).await?;
        }
        self.upload_source(&req.context_dir).await?;
        let build_id = self.start_build(req, &ecr_host).await?;
        self.wait_build(&build_id).await?;
        self.clients
            .s3
            .delete_object()
            .bucket(&self.settings.state_bucket)
            .key(&self.names.source_key)
            .send()
            .await
            .context("source zip の削除に失敗しました")?;
        println!("CodeBuild ビルド完了");
        Ok(())
    }

    // --- IAM ロール ---

    /// role を冪等に ensure して ARN を返す。既存 role はそのまま使う (policy の差分更新は
    /// しない)。external_roles では作らず、存在と信頼先だけを検査する
    async fn ensure_role(&self, region: &str, account_id: &str) -> Result<String> {
        let spec = codebuild_role(&CodeBuildRole {
            name: &self.names.role,
            region,
            account_id,
            state_bucket: &self.settings.state_bucket,
            project_name: &self.names.project,
            permissions_boundary: self.settings.permissions_boundary.as_deref(),
            external: self.settings.external_roles,
        });
        iam::ensure_role(&self.clients.iam, &spec).await
    }

    /// role を inline policy ごと削除する (無ければ何もしない)
    async fn delete_role(&self, name: &str) -> Result<()> {
        iam::delete_role(&self.clients.iam, name, &[]).await?;
        Ok(())
    }

    // --- CodeBuild プロジェクト ---

    async fn ensure_project(&self, platform: &str, role_arn: &str) -> Result<()> {
        let (env_type, image) = env_for_platform(platform);
        let project = &self.names.project;
        let source = ProjectSource::builder()
            .r#type(SourceType::S3)
            .location(format!(
                "{}/{}",
                self.settings.state_bucket, self.names.source_key
            ))
            .build()?;
        let artifacts = ProjectArtifacts::builder()
            .r#type(ArtifactsType::NoArtifacts)
            .build()?;
        let environment = ProjectEnvironment::builder()
            .r#type(env_type)
            .image(image)
            .compute_type(ComputeType::from(self.settings.compute_type.as_str()))
            .privileged_mode(true)
            .build()?;
        // batch_get_projects は存在しない名前でも例外にならず projectsNotFound に入るだけ
        let existing = self
            .clients
            .codebuild
            .batch_get_projects()
            .names(project)
            .send()
            .await
            .context("CodeBuild batch_get_projects に失敗しました")?;
        if !existing.projects().is_empty() {
            self.clients
                .codebuild
                .update_project()
                .name(project)
                .source(source)
                .artifacts(artifacts)
                .environment(environment)
                .service_role(role_arn)
                .send()
                .await
                .context("CodeBuild update_project に失敗しました")?;
            println!("  CodeBuildプロジェクト更新: {project}");
            return Ok(());
        }
        self.clients
            .codebuild
            .create_project()
            .name(project)
            .source(source)
            .artifacts(artifacts)
            .environment(environment)
            .service_role(role_arn)
            .send()
            .await
            .context("CodeBuild create_project に失敗しました")?;
        println!("  CodeBuildプロジェクト作成: {project}");
        Ok(())
    }

    // --- ソースアップロード ---

    async fn upload_source(&self, context_dir: &Path) -> Result<()> {
        println!("  ソースをS3にアップロード中...");
        let mut tmp = tempfile::NamedTempFile::new()?;
        write_source_zip(context_dir, tmp.as_file_mut())?;
        let size = tmp.as_file().metadata()?.len();
        let body = ByteStream::from_path(tmp.path())
            .await
            .context("source zip を読めません")?;
        self.clients
            .s3
            .put_object()
            .bucket(&self.settings.state_bucket)
            .key(&self.names.source_key)
            .body(body)
            .send()
            .await
            .context("source zip の upload に失敗しました")?;
        println!(
            "  アップロード完了 ({:.1} MB)",
            size as f64 / (1024.0 * 1024.0)
        );
        Ok(())
    }

    // --- ビルド実行 ---

    async fn start_build(&self, req: &BuildRequest, ecr_host: &str) -> Result<String> {
        let var = |name: &str, value: &str| {
            EnvironmentVariable::builder()
                .name(name)
                .value(value)
                .r#type(EnvironmentVariableType::Plaintext)
                .build()
        };
        let res = self
            .clients
            .codebuild
            .start_build()
            .project_name(&self.names.project)
            .buildspec_override(BUILDSPEC)
            .environment_variables_override(var("IMAGE_TAG", &req.target)?)
            .environment_variables_override(var("DOCKERFILE", &req.dockerfile_path)?)
            .environment_variables_override(var("ECR_HOST", ecr_host)?)
            .send()
            .await
            .context("CodeBuild start_build に失敗しました")?;
        let build_id = res
            .build_value()
            .and_then(|b| b.id())
            .context("CodeBuild start_build が build id を返しませんでした")?
            .to_string();
        println!("  ビルド開始: {build_id}");
        Ok(build_id)
    }

    async fn wait_build(&self, build_id: &str) -> Result<()> {
        println!("  ビルド完了を待機中...");
        loop {
            let res = self
                .clients
                .codebuild
                .batch_get_builds()
                .ids(build_id)
                .send()
                .await
                .context("CodeBuild batch_get_builds に失敗しました")?;
            let build = res
                .builds()
                .first()
                .context("CodeBuild batch_get_builds が build を返しませんでした")?;
            match build.build_status() {
                Some(StatusType::Succeeded) => return Ok(()),
                Some(
                    status @ (StatusType::Failed
                    | StatusType::Fault
                    | StatusType::Stopped
                    | StatusType::TimedOut),
                ) => {
                    let phases: Vec<(String, String, String)> = build
                        .phases()
                        .iter()
                        .map(|p| {
                            (
                                p.phase_type()
                                    .map(|t| t.as_str().to_string())
                                    .unwrap_or_else(|| "?".into()),
                                p.phase_status()
                                    .map(|s| s.as_str().to_string())
                                    .unwrap_or_else(|| "?".into()),
                                p.contexts()
                                    .first()
                                    .and_then(|c| c.message())
                                    .unwrap_or_default()
                                    .to_string(),
                            )
                        })
                        .collect();
                    print!("{}", failed_phases_report(&phases));
                    bail!(
                        "CodeBuild ビルド失敗: {build_id} (status={})",
                        status.as_str()
                    );
                }
                _ => tokio::time::sleep(POLL_INTERVAL).await,
            }
        }
    }
}

/// 失敗した phase の一覧 ((phase, status, message))。成功 / 不明の phase は載せない
pub fn failed_phases_report(phases: &[(String, String, String)]) -> String {
    let mut out = String::from("  --- CodeBuild フェーズ情報 ---\n");
    for (phase, status, message) in phases {
        if status != "SUCCEEDED" && status != "?" {
            out += &format!("  {phase}: {status} {message}\n");
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use std::io::{Cursor, Read};

    use super::*;

    #[test]
    fn names_follow_python() {
        let n = Names::new("dev-app-pocket-", false, "myorg-");
        assert_eq!(n.project, "dev-app-pocket-codebuild");
        assert_eq!(n.role, "dev-app-pocket-codebuild-role");
        assert_eq!(n.legacy_role, "forge-dev-app-pocket-codebuild-role");
        assert_eq!(
            n.source_key,
            "codebuild/dev-app-pocket-codebuild/source.zip"
        );
        let external = Names::new("dev-app-pocket-", true, "myorg-");
        assert_eq!(external.role, "myorg-codebuild-role");
        // prefix が空なら resource_prefix の名前のまま
        assert_eq!(Names::new("p-", true, "").role, "p-codebuild-role");
    }

    #[test]
    fn platform_selects_image() {
        assert_eq!(
            env_for_platform("linux/arm64"),
            (EnvironmentType::ArmContainer, IMAGE_ARM64)
        );
        assert_eq!(
            env_for_platform("linux/amd64"),
            (EnvironmentType::LinuxContainer, IMAGE_AMD64)
        );
    }

    #[test]
    fn buildspec_matches_python() {
        let expected = "version: 0.2\n\
            phases:\n\
            \x20 pre_build:\n\
            \x20   commands:\n\
            \x20     - >-\n\
            \x20       aws ecr get-login-password --region $AWS_DEFAULT_REGION\n\
            \x20       | docker login --username AWS\n\
            \x20       --password-stdin $ECR_HOST\n\
            \x20 build:\n\
            \x20   commands:\n\
            \x20     - docker build -t $IMAGE_TAG -f $DOCKERFILE .\n\
            \x20 post_build:\n\
            \x20   commands:\n\
            \x20     - docker push $IMAGE_TAG\n";
        assert_eq!(BUILDSPEC, expected);
    }

    #[test]
    fn boundary_env_overrides_config() {
        assert_eq!(
            resolve_permissions_boundary(Some("env".into()), Some("cfg")).as_deref(),
            Some("env")
        );
        assert_eq!(
            resolve_permissions_boundary(Some(String::new()), Some("cfg")).as_deref(),
            Some("cfg")
        );
        assert_eq!(resolve_permissions_boundary(None, None), None);
    }

    #[test]
    fn zip_normalizes_mode_and_skips_broken_symlink() {
        let tmp = tempfile::tempdir().unwrap();
        let d = tmp.path();
        std::fs::write(d.join("private.txt"), "p").unwrap();
        std::fs::set_permissions(
            d.join("private.txt"),
            std::fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        std::fs::write(d.join("run.sh"), "#!/bin/sh").unwrap();
        std::fs::set_permissions(d.join("run.sh"), std::fs::Permissions::from_mode(0o700)).unwrap();
        std::fs::create_dir(d.join(".git")).unwrap();
        std::fs::write(d.join(".git/HEAD"), "x").unwrap();
        std::os::unix::fs::symlink(d.join("missing"), d.join("broken")).unwrap();

        let mut buf = Cursor::new(Vec::new());
        assert_eq!(write_source_zip(d, &mut buf).unwrap(), 2);
        let mut archive = zip::ZipArchive::new(buf).unwrap();
        let mut seen = Vec::new();
        for i in 0..archive.len() {
            let mut f = archive.by_index(i).unwrap();
            let mut content = String::new();
            f.read_to_string(&mut content).unwrap();
            seen.push((
                f.name().unwrap().to_string(),
                f.unix_mode().unwrap() & 0o777,
                content,
            ));
        }
        assert_eq!(
            seen,
            [
                ("private.txt".to_string(), 0o644, "p".to_string()),
                ("run.sh".to_string(), 0o755, "#!/bin/sh".to_string()),
            ]
        );
    }

    #[test]
    fn failed_phases_only() {
        let phases = vec![
            ("SUBMITTED".into(), "SUCCEEDED".into(), String::new()),
            ("BUILD".into(), "FAILED".into(), "exit 1".into()),
            ("COMPLETED".into(), "?".into(), String::new()),
        ];
        assert_eq!(
            failed_phases_report(&phases),
            "  --- CodeBuild フェーズ情報 ---\n  BUILD: FAILED exit 1\n"
        );
    }
}
