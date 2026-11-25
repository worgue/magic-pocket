//! `pocket-rs django deploy` / `promote` / `deploystatic` / `storage upload` (Python 版
//! `pocket_cli/django_cli.py`)。
//!
//! collectstatic だけは project の Python で実行する (Python 版も subprocess)。storage の
//! 解決 ([`super::storage`]) と S3 への upload ([`crate::deploy::upload::sync_dir`]) は Rust で
//! 行い、AWS CLI は呼ばない。
//!
//! Python 版との違い:
//! - upload は `aws s3 sync` ではなく内容 (ETag とローカルの MD5) で差分を判定する。
//!   `aws s3 sync` はサイズと更新時刻で判定していた
//! - `--delete` で、ローカルの出力先にファイルが 1 つも無ければ S3 の object を消さずに
//!   エラーで止める
//! - `storage upload` は `aws s3 sync …` のコマンド行ではなく `<ローカル> -> s3://…` を出す

use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::{bail, Context as _, Result};
use aws_config::SdkConfig;
use magic_pocket_settings::context::{Context, GeneralContext};
use magic_pocket_settings::model::{PublishMode, StorageStore};

use super::storage::{
    self, env_container, get_storages, local_static_build, static_storage_s3_place,
    ResolvedStorage, StorageOptions, FILE_SYSTEM_STORAGE, S3_BOTO3_STORAGE,
};
use super::{manage, management_command_handler, payload, python_command, AwsManage};
use crate::aws_util::regional_config;
use crate::context::load_context;
use crate::deploy::interaction::{Interaction, Prompt};
use crate::deploy::pipeline::BeforeLambdaUpdate;
use crate::deploy::run::{deploy_url, open_browser, DeployArgs};
use crate::deploy::s3::bucket_exists;
use crate::deploy::upload::{sync_dir, SyncOptions, SyncResult};
use crate::status::sink::Sink;

/// 削除済みの `--skip-check-existing` の案内 (Python の `removed_skip_check_existing`)
pub const REMOVED_SKIP_CHECK_EXISTING: &str = "--skip-check-existing は 0.6.0 で廃止されました \
     (deploy は Neon に触れません)。[container.<name>.secrets.user] に接続 URL を type で宣言 \
     (例: `DATABASE_URL = { type = \"neon_database_url\" }`) したうえで、deploy 前に \
     `pocket resource neon store-url --stage <stage>` を一度実行してください。";

/// stage を指定しない `[general]` (Python の `GeneralContext.from_toml`)。
///
/// Python 版の storage の解決は stage があっても general の django_fallback と
/// s3_fallback_bucket_name をここから取る (stage の上書きは見ない)
fn general_context() -> Result<GeneralContext> {
    let toml = magic_pocket_rs::config::find_toml_path()?;
    Ok(magic_pocket_settings::Settings::load_general_context(
        &toml,
    )?)
}

/// deploy / promote の後処理で、staticfiles を deploystatic しない理由の案内
fn skip_message(context: &Context, stage: &str) -> Result<Option<String>> {
    let env = env_container();
    if storage::staticfiles_publish_mode(context, env.as_deref())? == PublishMode::Command {
        return Ok(Some(format!(
            "staticfiles is publish = \"command\": skipping deploystatic. \
             Publish with `pocket django deploystatic --stage {stage}`."
        )));
    }
    if storage::staticfiles_object_hash(context, env.as_deref())? {
        return Ok(Some(object_hash_skip_message(stage)));
    }
    Ok(None)
}

pub fn object_hash_skip_message(stage: &str) -> String {
    format!(
        "staticfiles is object_hash: skipping deploystatic. \
         Publish with `pocket-rs static publish --stage {stage}`."
    )
}

pub fn object_hash_deploystatic_error(stage: &str) -> String {
    format!(
        "staticfiles is object_hash: deploystatic does not support it. \
         Publish with `pocket-rs static publish --stage {stage}`."
    )
}

/// Lambda の更新の前に deploystatic してよい stage か (bucket の有無は別に確かめる)。
///
/// publish = "command"・object_hash・staticfiles が無い / S3 でない・`[s3]` が無い stage は
/// `false` (deploy の後の処理に任せる)
pub fn eligible_before_lambda_update(context: &Context) -> Result<bool> {
    let env = env_container();
    if storage::staticfiles_publish_mode(context, env.as_deref())? == PublishMode::Command
        || storage::staticfiles_object_hash(context, env.as_deref())?
    {
        return Ok(false);
    }
    let Some(staticfiles) = storage::container_staticfiles(context, env.as_deref())? else {
        return Ok(false);
    };
    Ok(staticfiles.store == StorageStore::S3 && context.s3.is_some())
}

/// static を Lambda の更新より先に upload する hook (Python の
/// `_deploystatic_before_lambda_update`)。
///
/// Lambda が新しい版の URL を出し始めた後に upload すると、その間に読まれたファイルは
/// upload 前の内容が新しい URL のキャッシュに入る。collectstatic の失敗も Lambda を
/// 更新する前に分かる。確認に No と答えても `handled` にする (deploy の後に聞き直さない)
pub struct StaticBeforeLambda {
    pub sdk: SdkConfig,
    pub stage: String,
    pub prompt: Interaction,
    pub handled: bool,
}

impl BeforeLambdaUpdate for StaticBeforeLambda {
    async fn before_lambda_update(&mut self, sink: &mut dyn Sink) -> Result<()> {
        let (context, toml_dir) = load_context(&self.stage)?;
        if !eligible_before_lambda_update(&context)? {
            return Ok(());
        }
        let s3 = context.s3.as_ref().expect("eligible なら [s3] がある");
        let client = aws_sdk_s3::Client::new(&regional_config(&self.sdk, &s3.region));
        if !bucket_exists(&client, &s3.bucket_name).await? {
            return Ok(());
        }
        if self.prompt.confirm("deploystatic?", true)? {
            let link = storage::staticfiles_link(&context, env_container().as_deref())?;
            collectstatic_locally(&context, &toml_dir, &self.stage, link, sink)?;
            upload_collected_staticfiles(&self.sdk, &context, &toml_dir, &self.stage, false, sink)
                .await?;
        }
        self.handled = true;
        Ok(())
    }
}

/// `django deploy` / `promote` の引数
pub struct DjangoDeployArgs {
    pub stage: String,
    pub promote: Option<String>,
    pub openpath: Option<String>,
    pub yes: bool,
    pub skip_migrate: bool,
    pub skip_check_existing: bool,
}

/// `django deploy` / `promote` (Python の `deploy` / `promote` + `_django_post_deploy`)
pub async fn deploy(args: DjangoDeployArgs, sink: &mut dyn Sink) -> Result<()> {
    if args.skip_check_existing {
        bail!("{REMOVED_SKIP_CHECK_EXISTING}");
    }
    let sdk = crate::aws_util::load_sdk().await;
    let prompt = Interaction {
        assume_yes: args.yes,
    };
    let mut hook = StaticBeforeLambda {
        sdk: sdk.clone(),
        stage: args.stage.clone(),
        prompt: Interaction {
            assume_yes: args.yes,
        },
        handled: false,
    };
    crate::deploy::run::run_with_hook(
        DeployArgs {
            stage: args.stage.clone(),
            promote: args.promote,
            openpath: None,
            skip_frontend: false,
            yes: args.yes,
        },
        &mut hook,
    )
    .await?;
    post_deploy(
        &sdk,
        &args.stage,
        &prompt,
        args.openpath.as_deref(),
        args.skip_migrate,
        hook.handled,
        sink,
    )
    .await
}

/// deploy / promote 共通の Django の後処理 (collectstatic + migrate + URL)。
///
/// `static_handled` は Lambda の更新の前に deploystatic を済ませたことを表し、ここでは
/// 繰り返さない。`skip_migrate` は migrate を確認ごと省く
pub async fn post_deploy(
    sdk: &SdkConfig,
    stage: &str,
    prompt: &Interaction,
    openpath: Option<&str>,
    skip_migrate: bool,
    static_handled: bool,
    sink: &mut dyn Sink,
) -> Result<()> {
    let (context, toml_dir) = load_context(stage)?;
    if static_handled {
    } else if let Some(message) = skip_message(&context, stage)? {
        sink.info(&message);
    } else if prompt.confirm("deploystatic?", true)? {
        let link = storage::staticfiles_link(&context, env_container().as_deref())?;
        collectstatic_locally(&context, &toml_dir, stage, link, sink)?;
        upload_collected_staticfiles(sdk, &context, &toml_dir, stage, false, sink).await?;
    }
    if skip_migrate {
        sink.info("--skip-migrate: migrate をスキップしました。");
    } else if prompt.confirm("migrate?", true)? {
        // handler の解決は migrate の実行が決まってから (management handler の無い stage で
        // deploy の成功後に止まり、URL の表示へ届かないのを防ぐ)
        let handler = management_command_handler(&context, sink)?;
        let api = AwsManage::new(sdk, &handler.region);
        manage(
            &api,
            handler,
            &payload("migrate", &[]),
            super::DEFAULT_TIMEOUT_SECONDS,
            sink,
        )
        .await?;
    }
    if let Some(url) = deploy_url(sdk, &context).await? {
        sink.success(&format!("url: {url}"));
        if let Some(path) = openpath {
            open_browser(&format!("{url}/{path}"));
        }
    }
    Ok(())
}

/// collectstatic の子 process に渡す環境変数 (親の環境変数に足す分)。
///
/// override の 2 つが親に既にあればエラー (Python の `set_staticfiles_override_env`)。
/// container の `DEPLOY_HASH` を渡し、settings.py の STATIC_URL を deploy 後と同じ値にする
/// (pocket の system check が Lambda の更新前の collectstatic で効くように)
pub fn collectstatic_env(
    context: &Context,
    backend: &str,
    location: &Path,
    parent: impl Fn(&str) -> Option<String>,
) -> Result<Vec<(String, String)>> {
    for name in [
        "POCKET_STATICFILES_BACKEND_OVERRIDE",
        "POCKET_STATICFILES_LOCATION_OVERRIDE",
    ] {
        if parent(name).is_some_and(|v| !v.is_empty()) {
            bail!("{name} is already set");
        }
    }
    let mut env = vec![
        (
            "POCKET_STATICFILES_BACKEND_OVERRIDE".to_string(),
            backend.to_string(),
        ),
        (
            "POCKET_STATICFILES_LOCATION_OVERRIDE".to_string(),
            location.to_string_lossy().into_owned(),
        ),
    ];
    let container = storage::resolve_django_container(context, env_container().as_deref())?;
    if let Some(hash) = container
        .and_then(|c| c.envs.get("DEPLOY_HASH"))
        .filter(|h| !h.is_empty())
    {
        env.push(("DEPLOY_HASH".to_string(), hash.clone()));
    }
    Ok(env)
}

/// collectstatic を実行するディレクトリ (django の `project_dir`、無ければ pocket.toml の場所)
fn django_dir(context: &Context, toml_dir: &Path) -> Result<PathBuf> {
    let container = storage::resolve_django_container(context, env_container().as_deref())?;
    Ok(
        match container
            .and_then(|c| c.django.as_ref())
            .and_then(|d| d.project_dir.as_deref())
        {
            Some(dir) => toml_dir.join(dir),
            None => toml_dir.to_path_buf(),
        },
    )
}

/// ローカルで collectstatic する (Python の `collectstatic_locally`)
pub fn collectstatic_locally(
    context: &Context,
    toml_dir: &Path,
    stage: &str,
    link: bool,
    sink: &mut dyn Sink,
) -> Result<()> {
    let general = general_context()?;
    let storages = get_storages(&general, Some(context), env_container().as_deref())?;
    let local = local_static_build(&storages, toml_dir, stage)?;
    if link {
        // link のときは出力先を毎回作り直す。link でない実体が混ざると collectstatic が
        // 全ファイルを実体でコピーし直し、元の削除で残った壊れた symlink は upload を
        // 失敗させる (link でないときは消さない = 実体の全量コピーを避ける)
        let _ = std::fs::remove_dir_all(&local.location);
    }
    sink.info(&format!("collectstatic to {}...", local.location.display()));
    let env = collectstatic_env(context, local.backend, &local.location, |name| {
        std::env::var(name).ok()
    })?;
    let mut args = vec!["manage.py", "collectstatic", "--noinput"];
    if link {
        args.push("--link");
    }
    let Some(mut command) = python_command() else {
        bail!("pythonが見つかりません。uvまたはpythonをインストールしてください。");
    };
    let display = command_line(&command, &args);
    let status = command
        .args(&args)
        .current_dir(django_dir(context, toml_dir)?)
        .envs(env)
        .status()
        .with_context(|| format!("{display} を起動できません"))?;
    if !status.success() {
        bail!(
            "Command '{display}' returned non-zero exit status {}.",
            status.code().unwrap_or(-1)
        );
    }
    Ok(())
}

fn command_line(command: &Command, args: &[&str]) -> String {
    let mut parts = vec![command.get_program().to_string_lossy().into_owned()];
    parts.extend(command.get_args().map(|a| a.to_string_lossy().into_owned()));
    parts.extend(args.iter().map(|a| a.to_string()));
    parts.join(" ")
}

fn print_sync_summary(result: &SyncResult, delete: bool, prefix: &str, sink: &mut dyn Sink) {
    let kept = if delete { "削除" } else { "旧版を保持" };
    sink.info(&format!(
        "{} ファイルをアップロードしました (skip {} / {kept} {}、prefix: {prefix})",
        result.uploaded,
        result.skipped,
        result.stale.len()
    ));
}

/// collectstatic の出力を staticfiles の S3 の置き場に同期する (Python の
/// `upload_collected_staticfiles`)
pub async fn upload_collected_staticfiles(
    sdk: &SdkConfig,
    context: &Context,
    toml_dir: &Path,
    stage: &str,
    delete: bool,
    sink: &mut dyn Sink,
) -> Result<()> {
    let general = general_context()?;
    let env = env_container();
    let place = static_storage_s3_place(&general, context, env.as_deref())?;
    let storages = get_storages(&general, Some(context), env.as_deref())?;
    let local = local_static_build(&storages, toml_dir, stage)?;
    sink.info(&format!("Bucket: {}", place.bucket_name));
    sink.info(&format!("Location: {}", place.location));
    sink.info("Uploading static files...");
    if !local.location.is_dir() {
        bail!(
            "{} がありません。collectstatic の出力先を確認してください。",
            local.location.display()
        );
    }
    let client = aws_sdk_s3::Client::new(&regional_config(sdk, &place.region));
    let options = SyncOptions {
        delete,
        refuse_empty_delete: true,
        ..SyncOptions::default()
    };
    let result = sync_dir(
        &client,
        &place.bucket_name,
        &local.location,
        &place.location,
        &|_| None,
        options,
        sink,
    )
    .await?;
    print_sync_summary(&result, delete, &place.location, sink);
    Ok(())
}

/// `django deploystatic`
pub async fn deploystatic(
    stage: &str,
    skip_collectstatic: bool,
    delete: bool,
    link: Option<bool>,
    sink: &mut dyn Sink,
) -> Result<()> {
    let sdk = crate::aws_util::load_sdk().await;
    let (context, toml_dir) = load_context(stage)?;
    if storage::staticfiles_object_hash(&context, env_container().as_deref())? {
        bail!("{}", object_hash_deploystatic_error(stage));
    }
    let link = match link {
        Some(link) => link,
        None => storage::staticfiles_link(&context, env_container().as_deref())?,
    };
    if !skip_collectstatic {
        collectstatic_locally(&context, &toml_dir, stage, link, sink)?;
    }
    upload_collected_staticfiles(&sdk, &context, &toml_dir, stage, delete, sink).await
}

fn find_storage<'s>(
    storages: &'s [(String, ResolvedStorage)],
    name: &str,
) -> Result<&'s ResolvedStorage> {
    storages
        .iter()
        .find(|(key, _)| key == name)
        .map(|(_, s)| s)
        .with_context(|| format!("storage '{name}' がありません"))
}

/// `storage upload` の元 (ローカルの dir) と先 (bucket, location)
pub fn upload_endpoints(
    from: &[(String, ResolvedStorage)],
    to: &[(String, ResolvedStorage)],
    name: &str,
) -> Result<(String, storage::S3Place)> {
    let (from, to) = (find_storage(from, name)?, find_storage(to, name)?);
    if from.backend != FILE_SYSTEM_STORAGE {
        bail!("Upload from only support FileSystemStorage");
    }
    if to.backend != S3_BOTO3_STORAGE {
        bail!("Upload to only support S3Boto3Storage");
    }
    let Some(StorageOptions::Filesystem {
        location: Some(location),
    }) = &from.options
    else {
        bail!("storage '{name}' の location がありません (stage 無しの storages に location を書いてください)");
    };
    let Some(StorageOptions::S3(place)) = &to.options else {
        bail!("Failed to build S3 options for {name}");
    };
    Ok((location.clone(), place.clone()))
}

/// `django storage upload <storage>`: stage 無しの FileSystemStorage を stage の
/// S3Boto3Storage に同期する
pub async fn storage_upload(
    stage: &str,
    name: &str,
    delete: bool,
    dryrun: bool,
    sink: &mut dyn Sink,
) -> Result<()> {
    let sdk = crate::aws_util::load_sdk().await;
    let (context, _toml_dir) = load_context(stage)?;
    let general = general_context()?;
    let env = env_container();
    // 元は stage 無し (Python の `get_storages()` と同じく POCKET_STAGE があればその stage)
    let pocket_stage = std::env::var("POCKET_STAGE").ok().filter(|s| !s.is_empty());
    let from_context = match pocket_stage.as_deref() {
        Some(s) if s == stage => Some(context.clone()),
        Some(s) => Some(load_context(s)?.0),
        None => None,
    };
    let from = get_storages(&general, from_context.as_ref(), env.as_deref())?;
    let to = get_storages(&general, Some(&context), env.as_deref())?;
    let (from_location, place) = upload_endpoints(&from, &to, name)?;
    sink.print(&format!(
        "{from_location} -> s3://{}/{}",
        place.bucket_name, place.location
    ));
    let client = aws_sdk_s3::Client::new(&regional_config(&sdk, &place.region));
    let options = SyncOptions {
        delete,
        refuse_empty_delete: true,
        exclude_hidden: true,
        dryrun,
    };
    let result = sync_dir(
        &client,
        &place.bucket_name,
        Path::new(&from_location),
        &place.location,
        &|_| None,
        options,
        sink,
    )
    .await?;
    if !dryrun {
        print_sync_summary(&result, delete, &place.location, sink);
    }
    Ok(())
}

/// 非表示の `django storages`: Python の `get_storages(stage=…)` のうち BACKEND・bucket_name・
/// location を JSON で出す (Python 版との照合テスト用)。`stage` が無ければ `get_storages()`
/// (stage 無し = general の django_fallback)
pub fn print_storages(stage: Option<&str>) -> Result<()> {
    let env = env_container();
    let general = general_context()?;
    let context = match stage {
        Some(stage) => Some(crate::context::load_context_read_only(stage)?.0),
        None => None,
    };
    let value = storages_json(&general, context.as_ref(), env.as_deref())?;
    println!("{}", magic_pocket_settings::pyjson::dumps_indent2(&value));
    Ok(())
}

fn storages_json(
    general: &GeneralContext,
    context: Option<&Context>,
    env: Option<&str>,
) -> Result<serde_json::Value> {
    let mut map = serde_json::Map::new();
    for (key, storage) in get_storages(general, context, env)? {
        map.insert(key, storage.to_json());
    }
    Ok(serde_json::Value::Object(map))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stack::testing::vector_context;

    #[test]
    fn test_collectstatic_env_refuses_existing_override() {
        let ctx = vector_context("toml_vectors/context_full.dev");
        let location = Path::new("/prj/pocket_cache/static_build/dev");
        let env = collectstatic_env(&ctx, "B", location, |_| None).expect("env");
        assert_eq!(
            env[..2],
            [
                (
                    "POCKET_STATICFILES_BACKEND_OVERRIDE".to_string(),
                    "B".to_string()
                ),
                (
                    "POCKET_STATICFILES_LOCATION_OVERRIDE".to_string(),
                    "/prj/pocket_cache/static_build/dev".to_string()
                ),
            ]
        );
        let error = collectstatic_env(&ctx, "B", location, |name| {
            (name == "POCKET_STATICFILES_LOCATION_OVERRIDE").then(|| "x".to_string())
        })
        .expect_err("既にある");
        assert_eq!(
            error.to_string(),
            "POCKET_STATICFILES_LOCATION_OVERRIDE is already set"
        );
        // 空の値は無いのと同じ (Python の真偽判定)
        assert!(collectstatic_env(&ctx, "B", location, |_| Some(String::new())).is_ok());
    }

    fn storages(
        entries: &[(&str, &'static str, Option<StorageOptions>)],
    ) -> Vec<(String, ResolvedStorage)> {
        entries
            .iter()
            .map(|(key, backend, options)| {
                (
                    key.to_string(),
                    ResolvedStorage {
                        backend,
                        options: options.clone(),
                    },
                )
            })
            .collect()
    }

    #[test]
    fn test_upload_endpoints_checks_backends() {
        let place = storage::S3Place {
            bucket_name: "b".into(),
            location: "media".into(),
            region: "r".into(),
        };
        let from = storages(&[(
            "media",
            FILE_SYSTEM_STORAGE,
            Some(StorageOptions::Filesystem {
                location: Some("data/media".into()),
            }),
        )]);
        let to = storages(&[(
            "media",
            S3_BOTO3_STORAGE,
            Some(StorageOptions::S3(place.clone())),
        )]);
        assert_eq!(
            upload_endpoints(&from, &to, "media").expect("ok"),
            ("data/media".to_string(), place.clone())
        );
        let error = upload_endpoints(&to, &to, "media").expect_err("元が S3");
        assert_eq!(
            error.to_string(),
            "Upload from only support FileSystemStorage"
        );
        let error = upload_endpoints(&from, &from, "media").expect_err("先が filesystem");
        assert_eq!(error.to_string(), "Upload to only support S3Boto3Storage");
        assert!(upload_endpoints(&from, &to, "other").is_err());
    }

    #[test]
    fn test_object_hash_messages_point_to_static_publish() {
        assert!(object_hash_skip_message("dev").contains("`pocket-rs static publish --stage dev`"));
        assert!(object_hash_deploystatic_error("dev")
            .contains("`pocket-rs static publish --stage dev`"));
    }
}
