//! IAM role の作成・検査・削除 (Python 版 `iam_roles.py` の API 経路と external_roles)。
//!
//! role の定義は [`RoleSpec`] (`stack::roles`)。ここは IAM を呼ぶ側だけを持つ。

use std::time::Duration;

use anyhow::{bail, Context as _, Result};
use aws_config::SdkConfig;
use aws_sdk_iam::Client;
use magic_pocket_settings::context::Context;
use serde_json::Value as Json;

use crate::aws_util::{has_code, regional_config, sdk_error};
use crate::stack::roles::{stage_role_specs, trusts_service, RoleSpec};

/// IAM role の伝播待ち (作成直後に使うと AssumeRole / PassRole が失敗する)
const ROLE_PROPAGATION_WAIT: Duration = Duration::from_secs(10);

const NO_SUCH_ENTITY: &[&str] = &["NoSuchEntity"];

/// role を冪等に ensure して ARN を返す。
///
/// 既存 role はそのまま返す (policy の差分更新はしない)。新規作成時は伝播待ちをしてから
/// 返す。external の role は作らず、検査して ARN を返す
pub async fn ensure_role(iam: &Client, spec: &RoleSpec) -> Result<String> {
    if spec.external {
        return verify_role(iam, spec).await;
    }
    let name = &spec.name;
    match iam.get_role().role_name(name).send().await {
        Ok(res) => {
            return res
                .role()
                .map(|r| r.arn().to_string())
                .context("IAM get_role が role を返しませんでした");
        }
        Err(e) if has_code(&e, NO_SUCH_ENTITY) => {}
        Err(e) => return Err(sdk_error(&format!("IAM get_role ({name})"), &e)),
    }
    eprintln!("Creating IAM role: {name}");
    let mut create = iam
        .create_role()
        .role_name(name)
        .assume_role_policy_document(spec.assume_role_policy().to_string());
    if let Some(boundary) = spec
        .permissions_boundary
        .as_deref()
        .filter(|b| !b.is_empty())
    {
        create = create.permissions_boundary(boundary);
    }
    let res = create
        .send()
        .await
        .map_err(|e| sdk_error(&format!("IAM create_role ({name})"), &e))?;
    let arn = res
        .role()
        .map(|r| r.arn().to_string())
        .context("IAM create_role が role を返しませんでした")?;
    for policy_arn in &spec.managed_policy_arns {
        iam.attach_role_policy()
            .role_name(name)
            .policy_arn(policy_arn)
            .send()
            .await
            .map_err(|e| sdk_error(&format!("IAM attach_role_policy ({name})"), &e))?;
    }
    for (policy_name, document) in &spec.inline_policies {
        iam.put_role_policy()
            .role_name(name)
            .policy_name(policy_name)
            .policy_document(document.to_string())
            .send()
            .await
            .map_err(|e| sdk_error(&format!("IAM put_role_policy ({name})"), &e))?;
    }
    eprintln!("Waiting for IAM role propagation...");
    tokio::time::sleep(ROLE_PROPAGATION_WAIT).await;
    Ok(arn)
}

/// role を付属の policy ごと削除する (無ければ `false`)。
///
/// managed policy は呼び出し側が渡したもの (= spec のもの) だけを外す。
/// ListAttachedRolePolicies は deploy 権限に無いため使わない。inline policy は一覧を
/// 取って全部消す
pub async fn delete_role(iam: &Client, name: &str, managed_policy_arns: &[String]) -> Result<bool> {
    for policy_arn in managed_policy_arns {
        match iam
            .detach_role_policy()
            .role_name(name)
            .policy_arn(policy_arn)
            .send()
            .await
        {
            Ok(_) => {}
            Err(e) if has_code(&e, NO_SUCH_ENTITY) => {}
            Err(e) => return Err(sdk_error(&format!("IAM detach_role_policy ({name})"), &e)),
        }
    }
    let listed = match iam.list_role_policies().role_name(name).send().await {
        Ok(res) => res,
        Err(e) if has_code(&e, NO_SUCH_ENTITY) => return Ok(false),
        Err(e) => return Err(sdk_error(&format!("IAM list_role_policies ({name})"), &e)),
    };
    for policy in listed.policy_names() {
        match iam
            .delete_role_policy()
            .role_name(name)
            .policy_name(policy)
            .send()
            .await
        {
            Ok(_) => {}
            Err(e) if has_code(&e, NO_SUCH_ENTITY) => return Ok(false),
            Err(e) => return Err(sdk_error(&format!("IAM delete_role_policy ({name})"), &e)),
        }
    }
    match iam.delete_role().role_name(name).send().await {
        Ok(_) => {}
        Err(e) if has_code(&e, NO_SUCH_ENTITY) => return Ok(false),
        Err(e) => return Err(sdk_error(&format!("IAM delete_role ({name})"), &e)),
    }
    eprintln!("Deleted IAM role: {name}");
    Ok(true)
}

/// external の role の存在と pocket の service への信頼を検査し、ARN を返す。
///
/// policy (managed / inline) の中身は検査しない。role の持ち主は組織の流儀 (標準の
/// 許可セット・命名・boundary) で policy を組むため、pocket の出力との文面一致を求めると、
/// pocket の版更新で文面が変わるたびに全 project の deploy が止まる
pub async fn verify_role(iam: &Client, spec: &RoleSpec) -> Result<String> {
    let name = &spec.name;
    let res = match iam.get_role().role_name(name).send().await {
        Ok(res) => res,
        Err(e) if has_code(&e, NO_SUCH_ENTITY) => bail!(
            "IAM role {name} がありません。[iam] external_roles = true では pocket は \
             role を作りません。`pocket permissions roles` の出力どおりに作成して\
             から再実行してください。"
        ),
        Err(e) => return Err(sdk_error(&format!("IAM get_role ({name})"), &e)),
    };
    let role = res
        .role()
        .context("IAM get_role が role を返しませんでした")?;
    // AssumeRolePolicyDocument は URL encode された JSON
    let document: Json = role
        .assume_role_policy_document()
        .and_then(|d| serde_json::from_str(&percent_decode(d)).ok())
        .unwrap_or(Json::Null);
    if !trusts_service(&document, &spec.service) {
        bail!(
            "IAM role {name} の信頼ポリシーが {} に sts:AssumeRole を許可していません。\
             `pocket permissions roles` の assume_role_policy を参考に信頼ポリシーを\
             直してから再実行してください。",
            spec.service
        );
    }
    Ok(role.arn().to_string())
}

/// external_roles の role がすべて存在し pocket の service を信頼しているかを、deploy の
/// 変更前に検査する (policy の中身は見ない)
pub async fn verify_stage_roles(sdk: &SdkConfig, ctx: &Context) -> Result<()> {
    let Some(general) = ctx.general.as_ref().filter(|_| ctx.external_roles) else {
        return Ok(());
    };
    let config = regional_config(sdk, &general.region);
    let identity = aws_sdk_sts::Client::new(&config)
        .get_caller_identity()
        .send()
        .await
        .map_err(|e| sdk_error("sts get_caller_identity", &e))?;
    let account_id = identity
        .account()
        .context("sts get_caller_identity が account を返しませんでした")?;
    let boundary_env = std::env::var("CODEBUILD_PERMISSIONS_BOUNDARY").ok();
    let iam = Client::new(&config);
    for spec in stage_role_specs(ctx, account_id, boundary_env.as_deref())? {
        verify_role(&iam, &spec).await?;
    }
    Ok(())
}

/// IAM が返す URL encode された policy 文書を戻す (`%XX` を 1 byte に戻す)
fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let Some(b) = s
                .get(i + 1..i + 3)
                .and_then(|hex| u8::from_str_radix(hex, 16).ok())
            {
                out.push(b);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_decode_policy_document() {
        assert_eq!(percent_decode("%7B%22a%22%3A1%7D"), "{\"a\":1}");
        assert_eq!(percent_decode("100%"), "100%");
    }
}
