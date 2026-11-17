//! ECR の書き込み (Python 版 `pocket_cli/resources/aws/ecr.py` の retag / sync / delete)。

use anyhow::{anyhow, Context as _, Result};
use aws_config::SdkConfig;
use aws_sdk_ecr::types::ImageIdentifier;
use magic_pocket_settings::context::ContainerContext;

use crate::aws_util::{has_code, sdk_error};
use crate::image_build::runner::Runner;

/// repository の URI。無ければ `None`
pub async fn repository_uri(ecr: &aws_sdk_ecr::Client, name: &str) -> Result<Option<String>> {
    // 全列挙は既定 100 件で切れるため名前指定で引く
    match ecr
        .describe_repositories()
        .repository_names(name)
        .send()
        .await
    {
        Ok(response) => Ok(response
            .repositories()
            .first()
            .and_then(|r| r.repository_uri())
            .map(str::to_string)),
        Err(e) if has_code(&e, &["RepositoryNotFoundException"]) => Ok(None),
        Err(e) => Err(sdk_error("ecr describe_repositories", &e)),
    }
}

/// `source_tag` の image に `dest_tag` を付ける (build しない)。
///
/// build once の昇格用: `:<commit hash>` の image へ `:<stage>` を移す。`dest_tag` が
/// 既に同じ image を指していれば何もしない
pub async fn retag(
    ecr: &aws_sdk_ecr::Client,
    name: &str,
    source_tag: &str,
    dest_tag: &str,
) -> Result<()> {
    let source = ImageIdentifier::builder().image_tag(source_tag).build();
    let response = match ecr
        .batch_get_image()
        .repository_name(name)
        .image_ids(source)
        .send()
        .await
    {
        Ok(response) => response,
        Err(e) if has_code(&e, &["RepositoryNotFoundException"]) => {
            return Err(anyhow!(
                "ECR repository '{name}' が存在しません。\
                 先に `pocket django build` を実行してください。"
            ))
        }
        Err(e) => return Err(sdk_error("ecr batch_get_image", &e)),
    };
    let Some(image) = response.images().first() else {
        return Err(anyhow!(
            "image :{source_tag} が ECR repository '{name}' に存在しません。\
             先に `pocket django build` を実行してください。"
        ));
    };
    let manifest = image
        .image_manifest()
        .context("ecr batch_get_image が imageManifest を返しませんでした")?;
    match ecr
        .put_image()
        .repository_name(name)
        .image_manifest(manifest)
        .image_tag(dest_tag)
        .send()
        .await
    {
        Ok(_) => Ok(()),
        // dest_tag が既に同じ digest を指している (再昇格しても同じ結果になる)
        Err(e) if has_code(&e, &["ImageAlreadyExistsException"]) => Ok(()),
        Err(e) => Err(sdk_error("ecr put_image", &e)),
    }
}

/// repository を image ごと消す。無ければ何もせず `false`
pub async fn delete_repository(ecr: &aws_sdk_ecr::Client, name: &str) -> Result<bool> {
    if repository_uri(ecr, name).await?.is_none() {
        return Ok(false);
    }
    ecr.delete_repository()
        .repository_name(name)
        .force(true)
        .send()
        .await
        .map_err(|e| sdk_error("ecr delete_repository", &e))?;
    Ok(true)
}

/// build の入力のうち container に依らないもの
pub struct BuildEnv<'a> {
    pub sdk: &'a SdkConfig,
    /// build context (pocket.toml のディレクトリ)
    pub context_dir: &'a std::path::Path,
    pub account_id: &'a str,
    /// CodeBuild backend の source 置き場。呼び出し側が用意しておく
    pub state_bucket: &'a str,
    pub state_bucket_region: &'a str,
    pub runner: &'a dyn Runner,
}

/// deploy の最初に行う image の用意。
///
/// 昇格 (`promote_commit_hash` あり) なら `:<hash>` の image に `:<stage>` を付け替え、
/// そうでなければ repository を用意して `:<stage>` で build & push する
pub async fn sync_image(env: &BuildEnv<'_>, c: &ContainerContext) -> Result<()> {
    if let Some(hash) = &c.promote_commit_hash {
        let ecr = aws_sdk_ecr::Client::new(&crate::aws_util::regional_config(env.sdk, &c.region));
        return retag(&ecr, &c.ecr_name, hash, &c.stage).await;
    }
    crate::image_build::build_container_image(
        env.sdk,
        c,
        env.context_dir,
        &c.stage,
        env.account_id,
        env.state_bucket,
        env.state_bucket_region,
        env.runner,
    )
    .await
}
