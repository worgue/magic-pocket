//! `pocket-rs context`: Python の `pocket context` と同じ JSON (マスク済み、indent 2)。
//!
//! Context の導出は `magic_pocket_settings::context` (純粋層) にあり、ここでは外部入力
//! ([`ExternalInputs`]) を AWS (Route53 / Secrets Manager)・git・ファイルシステムで
//! 実装する。AWS の client は必要になったときだけ作る (hosted zone も managed secret も
//! 引かない設定なら AWS の認証情報は要らない)。

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::OnceLock;

use anyhow::Result;
use aws_config::SdkConfig;
use magic_pocket_rs::config::find_toml_path;
use magic_pocket_settings::context::{Context, ContextInputs, ExternalInputs, HostedZone};
use magic_pocket_settings::{mask_secret_values, pyjson, Settings};
use tokio::runtime::Handle;

pub async fn print_context(stage: &str) -> Result<()> {
    let (context, _toml_dir) = load_context(stage)?;
    let masked = mask_secret_values(&context.to_json());
    println!("{}", pyjson::dumps_indent2(&masked));
    Ok(())
}

/// pocket.toml を探して stage の Context を作る (Python の `Context.from_toml`)。
///
/// 読み込み中の advisory と構築中の log / warning は stderr に出す。戻り値の 2 つ目は
/// pocket.toml のディレクトリ (相対パスの基準)。tokio の multi-thread runtime 上で呼ぶ
pub fn load_context(stage: &str) -> Result<(Context, PathBuf)> {
    load(stage, true)
}

/// [`load_context`] と同じだが、AWS に何も書かない (削除予約中の managed secret を
/// 復元せず、無いものとして扱う)。`status` 用
pub fn load_context_read_only(stage: &str) -> Result<(Context, PathBuf)> {
    load(stage, false)
}

fn load(stage: &str, restore_secrets: bool) -> Result<(Context, PathBuf)> {
    let toml_path = find_toml_path()?;
    let (settings, advisories) = Settings::load(&toml_path, stage)?;
    for advisory in advisories {
        eprintln!("{}", advisory.message);
    }
    let env: BTreeMap<String, String> = std::env::vars().collect();
    // managed_assets 等の相対パスは pocket.toml のディレクトリ基準
    let base_dir = toml_path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_default();
    let external = AwsExternal::new(Handle::current(), base_dir.clone(), restore_secrets);
    let inputs = ContextInputs {
        env: &env,
        external: &external,
    };
    // 外部入力の trait は同期 API なので、AWS 呼び出しは block_in_place で待つ
    let (context, messages) =
        tokio::task::block_in_place(|| Context::from_settings(&settings, &inputs))?;
    // Python 版の echo.log / echo.warning (stderr) と同じ文言
    for m in messages {
        eprintln!("{}", m.message);
    }
    Ok((context, base_dir))
}

struct AwsExternal {
    handle: Handle,
    config: OnceLock<SdkConfig>,
    /// pocket.toml のディレクトリ (dir_files の相対パスの基準)
    base_dir: PathBuf,
    /// 削除予約中の managed secret を復元してから読むか
    restore_secrets: bool,
}

impl AwsExternal {
    fn new(handle: Handle, base_dir: PathBuf, restore_secrets: bool) -> Self {
        Self {
            handle,
            config: OnceLock::new(),
            base_dir,
            restore_secrets,
        }
    }

    fn config(&self) -> &SdkConfig {
        self.config
            .get_or_init(|| self.handle.block_on(aws_config::load_from_env()))
    }
}

impl ExternalInputs for AwsExternal {
    fn hosted_zones(&self) -> Result<Vec<HostedZone>, String> {
        let client = aws_sdk_route53::Client::new(self.config());
        let res = self
            .handle
            .block_on(client.list_hosted_zones().send())
            .map_err(|e| format!("Route53 list_hosted_zones に失敗しました: {e:?}"))?;
        if res.is_truncated() {
            return Err(
                "Route53 hosted zone list is truncated. Please set hosted_zone_id.".to_string(),
            );
        }
        Ok(res
            .hosted_zones()
            .iter()
            .map(|z| HostedZone {
                name: z.name().to_string(),
                id: z.id().to_string(),
            })
            .collect())
    }

    /// Python の `SecretsManager._pocket_secrets_response` と同じく、削除予定
    /// (InvalidRequest) の secret は restore してから読む (read only のときは無い扱い)
    fn managed_secret_arn(&self, pocket_key: &str, region: &str) -> Result<Option<String>, String> {
        let config = self
            .config()
            .to_builder()
            .region(aws_config::Region::new(region.to_string()))
            .build();
        let client = aws_sdk_secretsmanager::Client::new(&config);
        let first = self
            .handle
            .block_on(client.get_secret_value().secret_id(pocket_key).send());
        match first {
            Ok(res) => Ok(res.arn().map(str::to_string)),
            Err(e) => {
                let service = e.into_service_error();
                if service.is_resource_not_found_exception() {
                    return Ok(None);
                }
                if !service.is_invalid_request_exception() {
                    return Err(format!(
                        "Secrets Manager get_secret_value ({pocket_key}) に失敗しました: {service:?}"
                    ));
                }
                if !self.restore_secrets {
                    return Ok(None);
                }
                self.handle
                    .block_on(client.restore_secret().secret_id(pocket_key).send())
                    .map_err(|e| format!("restore_secret ({pocket_key}) に失敗しました: {e:?}"))?;
                let res = self
                    .handle
                    .block_on(client.get_secret_value().secret_id(pocket_key).send())
                    .map_err(|e| {
                        format!("get_secret_value ({pocket_key}) に失敗しました: {e:?}")
                    })?;
                Ok(res.arn().map(str::to_string))
            }
        }
    }

    fn git_short_hash(&self) -> Option<String> {
        let out = Command::new("git")
            .args(["rev-parse", "--short", "HEAD"])
            .output()
            .ok()?;
        if !out.status.success() {
            return None;
        }
        Some(String::from_utf8_lossy(&out.stdout).trim().to_string())
    }

    fn dir_files(&self, dir: &str) -> Option<Vec<String>> {
        let path = self.base_dir.join(dir);
        if !path.is_dir() {
            return None;
        }
        let entries = std::fs::read_dir(&path).ok()?;
        Some(
            entries
                .filter_map(|e| e.ok())
                .filter(|e| e.path().is_file())
                .map(|e| e.file_name().to_string_lossy().into_owned())
                .collect(),
        )
    }
}
