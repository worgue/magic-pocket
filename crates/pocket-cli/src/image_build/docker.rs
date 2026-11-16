//! docker / depot backend (ローカルの docker CLI と depot CLI で build して push する)。
//!
//! Python 版 `builders/docker.py` / `builders/depot.py` の移植。どちらも build 前に ECR へ
//! `docker login` する (Python 版は python_on_whales の `login_ecr`)。

use std::collections::BTreeMap;

use anyhow::{bail, Context as _, Result};
use base64::Engine as _;

use super::context_files::warn_files_without_world_read;
use super::runner::{Cmd, Runner};
use super::BuildRequest;

/// ECR の認証 token で `docker login` する
pub async fn login_ecr(ecr: &aws_sdk_ecr::Client, runner: &dyn Runner) -> Result<()> {
    let res = ecr
        .get_authorization_token()
        .send()
        .await
        .context("ECR get_authorization_token に失敗しました")?;
    let Some(data) = res.authorization_data().first() else {
        bail!("ECR get_authorization_token が認証情報を返しませんでした");
    };
    let (token, endpoint) = match (data.authorization_token(), data.proxy_endpoint()) {
        (Some(t), Some(e)) => (t, e),
        _ => bail!("ECR get_authorization_token の応答に token / endpoint がありません"),
    };
    let (username, password) = decode_authorization_token(token)?;
    runner.run(&login_command(&username, endpoint).with_stdin(password))
}

/// `base64("<user>:<password>")` を分解する
fn decode_authorization_token(token: &str) -> Result<(String, String)> {
    let decoded = base64::engine::general_purpose::STANDARD
        .decode(token)
        .context("ECR の認証 token を base64 decode できません")?;
    let decoded = String::from_utf8(decoded).context("ECR の認証 token が UTF-8 ではありません")?;
    match decoded.split_once(':') {
        Some((user, password)) => Ok((user.to_string(), password.to_string())),
        None => bail!("ECR の認証 token の形式が想定と異なります"),
    }
}

fn login_command(username: &str, endpoint: &str) -> Cmd {
    Cmd::new(
        "docker",
        [
            "login",
            "--username",
            username,
            "--password-stdin",
            endpoint,
        ],
    )
}

fn dockerfile(req: &BuildRequest) -> String {
    req.context_dir
        .join(&req.dockerfile_path)
        .to_string_lossy()
        .into_owned()
}

/// `docker buildx build` (python_on_whales の `docker.build(push=True, provenance=False)`)
pub fn docker_build_command(req: &BuildRequest) -> Cmd {
    Cmd::new(
        "docker",
        [
            "buildx".to_string(),
            "build".to_string(),
            req.context_dir.to_string_lossy().into_owned(),
            "--file".to_string(),
            dockerfile(req),
            "--tag".to_string(),
            req.target.clone(),
            "--platform".to_string(),
            req.platform.clone(),
            "--provenance=false".to_string(),
            "--push".to_string(),
        ],
    )
}

pub async fn docker_build_and_push(
    req: &BuildRequest,
    ecr: &aws_sdk_ecr::Client,
    runner: &dyn Runner,
) -> Result<()> {
    // context は生の permission のまま image に入るため build 前に検査する
    // (codebuild backend は zip 時に正規化されるので不要)
    warn_files_without_world_read(&req.context_dir)?;
    println!("Building docker image...");
    println!("  context: {}", req.context_dir.display());
    println!("  dockerpath: {}", req.dockerfile_path);
    println!("  tags: {}", req.target);
    println!("  platforms: {}", req.platform);
    println!("Logging in to ecr...");
    login_ecr(ecr, runner).await?;
    println!("Pushing docker image...");
    runner.run(&docker_build_command(req))
}

/// depot に渡す環境変数。PATH / HOME / token / region / AWS の認証情報 / `DEPOT_*` だけを
/// 渡す (Python 版と同じ。親の環境は引き継がない)
pub fn depot_env(
    parent: &BTreeMap<String, String>,
    region: &str,
    token: &str,
    project_id: Option<&str>,
) -> BTreeMap<String, String> {
    let mut env = BTreeMap::new();
    for key in ["PATH", "HOME"] {
        env.insert(
            key.to_string(),
            parent.get(key).cloned().unwrap_or_default(),
        );
    }
    env.insert("DEPOT_TOKEN".into(), token.into());
    env.insert("AWS_DEFAULT_REGION".into(), region.into());
    env.insert("AWS_REGION".into(), region.into());
    for key in [
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ] {
        if let Some(value) = parent.get(key) {
            env.insert(key.into(), value.clone());
        }
    }
    for (key, value) in parent {
        if key.starts_with("DEPOT_") && !env.contains_key(key) {
            env.insert(key.clone(), value.clone());
        }
    }
    if let Some(id) = project_id {
        env.insert("DEPOT_PROJECT_ID".into(), id.into());
    }
    env
}

pub fn depot_build_command(req: &BuildRequest, project_id: Option<&str>) -> Cmd {
    let mut args = vec![
        "build".to_string(),
        req.context_dir.to_string_lossy().into_owned(),
        "--file".to_string(),
        dockerfile(req),
        "--tag".to_string(),
        req.target.clone(),
        "--platform".to_string(),
        req.platform.clone(),
        "--push".to_string(),
        "--provenance=false".to_string(),
    ];
    if let Some(id) = project_id {
        args.extend(["--project".to_string(), id.to_string()]);
    }
    Cmd::new("depot", args)
}

/// `[container.<name>.build] depot_project_id`、無ければ `DEPOT_PROJECT_ID`
pub fn depot_project_id(
    configured: Option<&str>,
    parent: &BTreeMap<String, String>,
) -> Option<String> {
    configured
        .map(str::to_string)
        .or_else(|| parent.get("DEPOT_PROJECT_ID").cloned())
        .filter(|id| !id.is_empty())
}

pub async fn depot_build_and_push(
    req: &BuildRequest,
    configured_project_id: Option<&str>,
    ecr: &aws_sdk_ecr::Client,
    runner: &dyn Runner,
) -> Result<()> {
    let parent: BTreeMap<String, String> = std::env::vars().collect();
    let project_id = depot_project_id(configured_project_id, &parent);
    let Some(token) = parent.get("DEPOT_TOKEN").filter(|t| !t.is_empty()) else {
        bail!("DEPOT_TOKEN 環境変数が設定されていません");
    };
    // context は生の permission のまま image に入るため build 前に検査する
    warn_files_without_world_read(&req.context_dir)?;

    println!("ECR にログインします...");
    login_ecr(ecr, runner).await?;

    println!("Depot でイメージをビルドします...");
    println!("  target: {}", req.target);
    println!("  context: {}", req.context_dir.display());
    println!("  dockerfile: {}", req.dockerfile_path);
    println!("  platform: {}", req.platform);
    if let Some(id) = &project_id {
        println!("  project: {id}");
    }
    let env = depot_env(&parent, &req.region, token, project_id.as_deref());
    runner.run(&depot_build_command(req, project_id.as_deref()).with_env(env))?;
    println!("Depot ビルド完了");
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use super::*;

    fn req() -> BuildRequest {
        BuildRequest {
            target: "123.dkr.ecr.ap-northeast-1.amazonaws.com/repo:abc".into(),
            context_dir: PathBuf::from("/proj"),
            dockerfile_path: "docker/pocket.Dockerfile".into(),
            platform: "linux/arm64".into(),
            region: "ap-northeast-1".into(),
        }
    }

    #[test]
    fn decode_token() {
        let token = base64::engine::general_purpose::STANDARD.encode("AWS:pa:ss");
        assert_eq!(
            decode_authorization_token(&token).unwrap(),
            ("AWS".to_string(), "pa:ss".to_string())
        );
        assert!(decode_authorization_token("!!").is_err());
    }

    #[test]
    fn login_passes_password_by_stdin() {
        let cmd = login_command("AWS", "https://123.dkr.ecr.ap-northeast-1.amazonaws.com");
        assert_eq!(
            cmd.args,
            [
                "login",
                "--username",
                "AWS",
                "--password-stdin",
                "https://123.dkr.ecr.ap-northeast-1.amazonaws.com"
            ]
        );
    }

    #[test]
    fn docker_command() {
        let cmd = docker_build_command(&req());
        assert_eq!(cmd.program, "docker");
        assert_eq!(
            cmd.args,
            [
                "buildx",
                "build",
                "/proj",
                "--file",
                "/proj/docker/pocket.Dockerfile",
                "--tag",
                "123.dkr.ecr.ap-northeast-1.amazonaws.com/repo:abc",
                "--platform",
                "linux/arm64",
                "--provenance=false",
                "--push",
            ]
        );
        assert_eq!(cmd.env, None);
    }

    #[test]
    fn depot_command_and_project() {
        let cmd = depot_build_command(&req(), Some("p1"));
        assert_eq!(cmd.program, "depot");
        assert_eq!(&cmd.args[..2], ["build", "/proj"]);
        assert_eq!(
            &cmd.args[cmd.args.len() - 4..],
            ["--push", "--provenance=false", "--project", "p1"]
        );
        let without = depot_build_command(&req(), None);
        assert_eq!(without.args.last().unwrap(), "--provenance=false");
    }

    #[test]
    fn depot_project_id_prefers_config() {
        let mut parent = BTreeMap::new();
        assert_eq!(depot_project_id(None, &parent), None);
        parent.insert("DEPOT_PROJECT_ID".to_string(), "env".to_string());
        assert_eq!(depot_project_id(None, &parent).as_deref(), Some("env"));
        assert_eq!(
            depot_project_id(Some("cfg"), &parent).as_deref(),
            Some("cfg")
        );
    }

    #[test]
    fn depot_env_is_allowlisted() {
        let parent: BTreeMap<String, String> = [
            ("PATH", "/bin"),
            ("HOME", "/home/u"),
            ("AWS_ACCESS_KEY_ID", "AK"),
            ("AWS_PROFILE", "dev"),
            ("DEPOT_TOKEN", "parent-token"),
            ("DEPOT_DEBUG", "1"),
            ("SECRET", "x"),
        ]
        .into_iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();
        let env = depot_env(&parent, "us-east-1", "tok", Some("p1"));
        let expected: BTreeMap<String, String> = [
            ("AWS_ACCESS_KEY_ID", "AK"),
            ("AWS_DEFAULT_REGION", "us-east-1"),
            ("AWS_REGION", "us-east-1"),
            ("DEPOT_DEBUG", "1"),
            ("DEPOT_PROJECT_ID", "p1"),
            ("DEPOT_TOKEN", "tok"),
            ("HOME", "/home/u"),
            ("PATH", "/bin"),
        ]
        .into_iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();
        assert_eq!(env, expected);
    }
}
