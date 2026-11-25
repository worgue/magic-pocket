//! `pocket permissions`: Python 版 `pocket permissions` (`pocket_cli/cli/permissions_cli.py`)。
//!
//! - `list`: pocket.toml の構成から deploy に必要な AWS IAM Action を出す
//!   (表と判定は Python の `pocket/permissions.py` の写し)
//! - `roles`: pocket が使う IAM role の定義を JSON で出す (`[iam] external_roles` で role を
//!   事前に作る用途)
//! - `role-names`: external_roles で事前に作る role の名前を種別ごとに JSON で出す
//!
//! Python の表や判定を変えたら `just vectors` で `tests/data/permissions_vectors` を作り直し、
//! ここを直す (このファイルのテストが vector と一致を確かめる)。

use anyhow::{bail, Context as _, Result};
use clap::{Subcommand, ValueEnum};
use magic_pocket_rs::config::find_toml_path;
use magic_pocket_settings::context::Context;
use magic_pocket_settings::model::{BuildBackend, StoreType};
use magic_pocket_settings::{pyjson, Settings};
use serde_json::{json, Map, Value as Json};

use crate::aws_util::{load_sdk, regional_config, sdk_error};
use crate::stack::roles::stage_role_specs;

#[derive(Subcommand)]
pub enum PermissionsCommand {
    /// pocket.toml から必要な AWS Action 一覧を出力する。
    ///
    /// docs/permissions/aws.md のテーブルに基づき、`[cloudfront]` / `[rds]` / `[ses]` などの
    /// 設定有無に応じて必要 Action を組み立てる。粒度はワイルドカード中心
    /// (`cloudformation:*` 等)。
    List {
        /// stage 名
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        /// 出力形式。text: 1 行 1 Action / json: {"actions": [...]}
        #[arg(long, value_enum, default_value_t = ListFormat::Text)]
        format: ListFormat,
    },
    /// pocket が使う IAM role の定義を JSON で出力する。
    ///
    /// role ごとに名前・ARN・信頼ポリシー・managed policy・inline policy を出す。policy 中の
    /// CloudFormation 変数 (${AWS::Region} 等) は展開済みなので、そのまま IAM の CreateRole /
    /// PutRolePolicy に渡せる。`[iam] external_roles = true` の stage では、deploy がこの
    /// 定義と実際の role を突き合わせる。
    Roles {
        /// stage 名
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        /// role の ARN と policy に埋め込む AWS account ID。省略時は sts:GetCallerIdentity で
        /// 取得する。
        #[arg(long)]
        account_id: Option<String>,
    },
    /// [iam] external_roles で事前に作る role の名前を種別ごとに JSON で出力する。
    ///
    /// AWS には問い合わせない。規則は `{stage}-{project}-{namespace}-{kind}-role` (namespace は
    /// [iam] namespace、無ければ [general] namespace) で、互換を保つ公開仕様。使わない種別も
    /// 常に出す。
    RoleNames {
        /// stage 名
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
}

#[derive(Clone, Copy, ValueEnum)]
pub enum ListFormat {
    Text,
    Json,
}

pub async fn run(command: PermissionsCommand) -> Result<()> {
    match command {
        PermissionsCommand::List { stage, format } => {
            print!("{}", list_output(&load_settings(&stage)?, format));
        }
        PermissionsCommand::RoleNames { stage } => {
            print!("{}", role_names_output(&load_settings(&stage)?));
        }
        PermissionsCommand::Roles { stage, account_id } => {
            let (ctx, _) = crate::context::load_context(&stage)?;
            let Some(general) = ctx.general.as_ref() else {
                bail!("[general] が見つかりません");
            };
            let account = match account_id {
                Some(account) => account,
                None => caller_account(&general.region).await?,
            };
            let boundary_env = std::env::var("CODEBUILD_PERMISSIONS_BOUNDARY").ok();
            print!(
                "{}",
                roles_output(&ctx, &stage, &account, boundary_env.as_deref())?
            );
        }
    }
    Ok(())
}

fn load_settings(stage: &str) -> Result<Settings> {
    let toml_path = find_toml_path()?;
    let (settings, advisories) = Settings::load(&toml_path, stage)?;
    for advisory in advisories {
        eprintln!("{}", advisory.message);
    }
    Ok(settings)
}

async fn caller_account(region: &str) -> Result<String> {
    let sdk = load_sdk().await;
    let identity = aws_sdk_sts::Client::new(&regional_config(&sdk, region))
        .get_caller_identity()
        .send()
        .await
        .map_err(|e| sdk_error("sts get_caller_identity", &e))?;
    Ok(identity
        .account()
        .context("sts get_caller_identity が account を返しませんでした")?
        .to_string())
}

/// `list` の stdout (末尾の改行込み)
pub fn list_output(settings: &Settings, format: ListFormat) -> String {
    let actions = compute_actions(settings);
    match format {
        ListFormat::Json => format!("{}\n", pyjson::dumps_indent2(&json!({"actions": actions}))),
        ListFormat::Text => actions.iter().map(|a| format!("{a}\n")).collect(),
    }
}

/// `role-names` の stdout (末尾の改行込み)
pub fn role_names_output(settings: &Settings) -> String {
    let names: Map<String, Json> = external_role_names(settings)
        .into_iter()
        .map(|(kind, name)| (kind.to_string(), Json::String(name)))
        .collect();
    format!("{}\n", pyjson::dumps_indent2(&Json::Object(names)))
}

/// `roles` の stdout (末尾の改行込み)。`codebuild_boundary_env` は
/// `CODEBUILD_PERMISSIONS_BOUNDARY` の値
pub fn roles_output(
    ctx: &Context,
    stage: &str,
    account_id: &str,
    codebuild_boundary_env: Option<&str>,
) -> Result<String> {
    let Some(general) = ctx.general.as_ref() else {
        bail!("[general] が見つかりません");
    };
    let region = &general.region;
    let roles = stage_role_specs(ctx, account_id, codebuild_boundary_env)?
        .iter()
        .map(|spec| {
            Ok(json!({
                "name": spec.name,
                "arn": format!("arn:aws:iam::{account_id}:role/{}", spec.name),
                "assume_role_policy": spec.assume_role_policy(),
                "managed_policy_arns": spec.managed_policy_arns,
                "inline_policies": spec.resolved_inline_policies(region, account_id)?,
            }))
        })
        .collect::<Result<Vec<Json>>>()?;
    let output = json!({
        "stage": stage,
        "region": region,
        "account_id": account_id,
        "external_roles": ctx.external_roles,
        "roles": roles,
    });
    Ok(format!("{}\n", pyjson::dumps_indent2_unicode(&output)))
}

// ---- action group の表 (Python の `pocket/permissions.py` の写し) ----

const CORE_ACTIONS: &[&str] = &[
    "cloudformation:*",
    "ecr:*",
    "lambda:*",
    "apigateway:*",
    "s3:*",
    "iam:CreateRole",
    "iam:DeleteRole",
    "iam:GetRole",
    "iam:PutRolePolicy",
    "iam:DeleteRolePolicy",
    "iam:AttachRolePolicy",
    "iam:DetachRolePolicy",
    "iam:PassRole",
    "iam:TagRole",
    "iam:UntagRole",
    "iam:ListRoleTags",
    "iam:ListRolePolicies",
    "logs:*",
    "sts:GetCallerIdentity",
];

const SM_ACTIONS: &[&str] = &["secretsmanager:*"];

const SSM_ACTIONS: &[&str] = &[
    "ssm:GetParameter",
    "ssm:PutParameter",
    "ssm:DeleteParameter",
    "ssm:DeleteParameters",
    "ssm:GetParametersByPath",
];

const CLOUDFRONT_ACTIONS: &[&str] = &[
    "cloudfront:*",
    "cloudfront-keyvaluestore:*",
    "acm:RequestCertificate",
    "acm:DescribeCertificate",
    "acm:DeleteCertificate",
    "route53:ListHostedZones",
    "route53:ChangeResourceRecordSets",
    "route53:GetChange",
];

const WAF_ACTIONS: &[&str] = &["wafv2:*"];

const VPC_ACTIONS: &[&str] = &["ec2:*"];

const RDS_ACTIONS: &[&str] = &[
    "rds:*",
    "ec2:*SecurityGroup*",
    "ssm:GetParameter",
    "ssm:PutParameter",
    "ssm:DeleteParameter",
    "secretsmanager:DescribeSecret",
    "secretsmanager:RotateSecret",
];

const EFS_ACTIONS: &[&str] = &["elasticfilesystem:*"];

const SQS_ACTIONS: &[&str] = &["sqs:*"];

const SQS_ALERT_ACTIONS: &[&str] = &["sns:*", "cloudwatch:*"];

const SES_ACTIONS: &[&str] = &["ses:SendEmail", "ses:SendRawEmail"];

const CODEBUILD_ACTIONS: &[&str] = &["codebuild:*"];

const DSQL_ACTIONS: &[&str] = &["dsql:*"];

const BACKUP_ACTIONS: &[&str] = &[
    "backup:*",
    "backup-storage:MountCapsule",
    "kms:CreateGrant",
    "kms:Decrypt",
    "kms:DescribeKey",
    "kms:GenerateDataKey",
    "kms:RetireGrant",
];

const EXTERNAL_ROLES_ACTIONS: &[&str] = &[];

/// external_roles では不要になる core の IAM 系 (role の作成・更新・削除)
const IAM_ROLE_WRITE_ACTIONS: &[&str] = &[
    "iam:CreateRole",
    "iam:DeleteRole",
    "iam:PutRolePolicy",
    "iam:DeleteRolePolicy",
    "iam:AttachRolePolicy",
    "iam:DetachRolePolicy",
    "iam:TagRole",
    "iam:UntagRole",
    "iam:ListRoleTags",
    "iam:ListRolePolicies",
];

const SCHEDULER_ACTIONS: &[&str] = &["scheduler:*"];

const TAG_ACTIONS: &[&str] = &["tag:TagResources", "tag:UntagResources"];

const INBOUND_ACTIONS: &[&str] = &[
    "ses:DescribeActiveReceiptRuleSet",
    "ses:DescribeReceiptRuleSet",
    "ses:DescribeReceiptRule",
    "ses:CreateReceiptRule",
    "ses:UpdateReceiptRule",
    "ses:DeleteReceiptRule",
    "ses:SetReceiptRulePosition",
    "ses:GetEmailIdentity",
    "ses:CreateEmailIdentity",
    "ses:DeleteEmailIdentity",
    "ses:PutEmailIdentityDkimAttributes",
    "ses:PutEmailIdentityDkimSigningAttributes",
    "ses:PutEmailIdentityFeedbackAttributes",
    "ses:PutEmailIdentityMailFromAttributes",
    "ses:PutEmailIdentityConfigurationSetAttributes",
    "ses:TagResource",
    "ses:UntagResource",
    "route53:ListHostedZones",
    "route53:ChangeResourceRecordSets",
    "route53:GetChange",
];

/// group 名と Action (Python の `action_groups()` と同じ順)
const ACTION_GROUPS: &[(&str, &[&str])] = &[
    ("core", CORE_ACTIONS),
    ("ssm", SSM_ACTIONS),
    ("secretsmanager", SM_ACTIONS),
    ("cloudfront", CLOUDFRONT_ACTIONS),
    ("waf", WAF_ACTIONS),
    ("vpc", VPC_ACTIONS),
    ("rds", RDS_ACTIONS),
    ("efs", EFS_ACTIONS),
    ("sqs", SQS_ACTIONS),
    ("sqs_alert", SQS_ALERT_ACTIONS),
    ("ses", SES_ACTIONS),
    ("inbound", INBOUND_ACTIONS),
    ("codebuild", CODEBUILD_ACTIONS),
    ("dsql", DSQL_ACTIONS),
    ("backup", BACKUP_ACTIONS),
    ("scheduler", SCHEDULER_ACTIONS),
    ("tag", TAG_ACTIONS),
    ("external_roles", EXTERNAL_ROLES_ACTIONS),
];

/// `[iam] external_roles` で利用者が作る role の種別 (名前は `{prefix}{kind}-role`)
const EXTERNAL_ROLE_KINDS: &[&str] = &["lambda", "scheduler", "codebuild", "backup"];

/// secrets の実効 store 集合 (グローバル既定 + user secret の per-spec override)
fn effective_secret_stores(settings: &Settings) -> Vec<StoreType> {
    let mut stores = Vec::new();
    for c in settings.container.values() {
        let Some(secrets) = &c.secrets else { continue };
        stores.push(secrets.store);
        for spec in secrets.user.values() {
            stores.push(spec.store.unwrap_or(secrets.store));
        }
    }
    stores
}

/// settings で付与する group の名前 (Python の `_enabled_groups`。`ACTION_GROUPS` の順)
pub fn enabled_groups(settings: &Settings) -> Vec<&'static str> {
    let containers = || settings.container.values();
    let handlers = || containers().flat_map(|c| c.handlers.values());
    let stores = effective_secret_stores(settings);
    let has_sqs_alert = handlers().any(|h| {
        h.sqs
            .as_ref()
            .and_then(|s| s.dead_letter_alert.as_ref())
            .is_some_and(|a| a.enabled)
    });
    let uses_backup =
        settings.dsql.is_some() || settings.backup.as_ref().is_some_and(|b| b.rds.is_some());
    let condition = |name: &str| match name {
        "core" => true,
        "ssm" => stores.contains(&StoreType::Ssm),
        // secrets 未設定でも従来どおり secretsmanager group を含める
        "secretsmanager" => stores.is_empty() || stores.contains(&StoreType::Sm),
        "cloudfront" => !settings.cloudfront.is_empty(),
        "waf" => settings.cloudfront.values().any(|cf| cf.waf.is_some()),
        "vpc" => containers().any(|c| c.vpc.is_some()),
        "rds" => settings.rds.is_some(),
        "efs" => containers().any(|c| c.vpc.as_ref().is_some_and(|v| v.efs.is_some())),
        "sqs" => handlers().any(|h| h.sqs.is_some()),
        "sqs_alert" => has_sqs_alert || !settings.inbound.is_empty(),
        "ses" => settings.ses.is_some(),
        "inbound" => !settings.inbound.is_empty(),
        "codebuild" => containers().any(|c| c.build.backend == BuildBackend::Codebuild),
        "dsql" => settings.dsql.is_some(),
        "backup" => uses_backup,
        "scheduler" => settings
            .scheduler
            .as_ref()
            .is_some_and(|s| !s.schedules.is_empty()),
        "tag" => containers().any(|c| c.vpc.as_ref().is_some_and(|v| !v.manage)),
        "external_roles" => settings.iam.external_roles,
        _ => unreachable!("未知の group: {name}"),
    };
    ACTION_GROUPS
        .iter()
        .map(|(name, _)| *name)
        .filter(|name| condition(name))
        .collect()
}

/// settings から必要な AWS Action 一覧を算出する (Python の `compute_actions`)。
/// 重複は最初の出現位置を保ったまま除去する
pub fn compute_actions(settings: &Settings) -> Vec<&'static str> {
    let excluded: &[&str] = if settings.iam.external_roles {
        IAM_ROLE_WRITE_ACTIONS
    } else {
        &[]
    };
    let mut out: Vec<&'static str> = Vec::new();
    for name in enabled_groups(settings) {
        let (_, actions) = ACTION_GROUPS
            .iter()
            .find(|(n, _)| *n == name)
            .expect("group");
        for action in *actions {
            if !excluded.contains(action) && !out.contains(action) {
                out.push(action);
            }
        }
    }
    out
}

/// `[iam] external_roles` で利用者が事前に作る role の名前を種別ごとに返す
/// (Python の `external_role_names`)
pub fn external_role_names(settings: &Settings) -> Vec<(&'static str, String)> {
    let prefix = settings.external_role_prefix();
    EXTERNAL_ROLE_KINDS
        .iter()
        .map(|kind| (*kind, format!("{prefix}{kind}-role")))
        .collect()
}

#[cfg(test)]
mod tests {
    use std::path::{Path, PathBuf};

    use magic_pocket_settings::LoadContext;

    use super::*;
    use crate::stack::testing::{context, read_json, read_text, repo_root};

    fn vector_paths() -> Vec<PathBuf> {
        fn walk(dir: &Path, out: &mut Vec<PathBuf>) {
            for entry in std::fs::read_dir(dir).unwrap_or_else(|e| panic!("{}: {e}", dir.display()))
            {
                let path = entry.expect("dir entry").path();
                if path.is_dir() {
                    walk(&path, out);
                } else if path.extension().is_some_and(|e| e == "json") {
                    out.push(path);
                }
            }
        }
        let mut out = Vec::new();
        walk(
            &repo_root().join("tests/data/permissions_vectors"),
            &mut out,
        );
        out.sort();
        out
    }

    fn settings(vector: &Json) -> Settings {
        let input = repo_root().join(vector["input"].as_str().expect("input"));
        let load = LoadContext {
            project_name_fallback: vector["context"]["project_name_fallback"]
                .as_str()
                .expect("project_name_fallback")
                .to_string(),
        };
        let stage = vector["stage"].as_str().expect("stage");
        Settings::from_parts(&read_text(&input), stage, &load)
            .unwrap_or_else(|e| panic!("{}: {e}", input.display()))
            .0
    }

    #[test]
    fn test_outputs_match_python() {
        let paths = vector_paths();
        assert!(!paths.is_empty(), "permissions vector がありません");
        let mut roles_checked = 0;
        for path in paths {
            let vector = read_json(&path);
            let name = path.display();
            let settings = settings(&vector);
            assert_eq!(
                json!(enabled_groups(&settings)),
                vector["groups"],
                "{name}: groups"
            );
            assert_eq!(
                list_output(&settings, ListFormat::Text),
                vector["list_text"].as_str().expect("list_text"),
                "{name}: list"
            );
            assert_eq!(
                list_output(&settings, ListFormat::Json),
                vector["list_json"].as_str().expect("list_json"),
                "{name}: list --format json"
            );
            assert_eq!(
                role_names_output(&settings),
                vector["role_names"].as_str().expect("role_names"),
                "{name}: role-names"
            );
            let Some(expected) = vector["roles"].as_str() else {
                continue;
            };
            let ctx = context(&repo_root(), &vector);
            let stage = vector["stage"].as_str().expect("stage");
            let account = vector["account_id"].as_str().expect("account_id");
            let actual = roles_output(&ctx, stage, account, None).expect("roles");
            assert_eq!(actual, expected, "{name}: roles");
            roles_checked += 1;
        }
        assert!(roles_checked > 0, "roles の vector がありません");
    }

    #[test]
    fn test_external_roles_drop_iam_writes() {
        let ctx_toml = "[general]\nregion = \"ap-northeast-1\"\nproject_name = \"p\"\n\
                        stages = [\"dev\"]\n\n[iam]\nexternal_roles = true\n";
        let load = LoadContext {
            project_name_fallback: "p".into(),
        };
        let (settings, _) = Settings::from_parts(ctx_toml, "dev", &load).expect("settings");
        let actions = compute_actions(&settings);
        assert!(actions.contains(&"iam:GetRole"));
        assert!(actions.contains(&"iam:PassRole"));
        assert!(!actions.contains(&"iam:CreateRole"));
    }
}
