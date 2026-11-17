//! pocket が作る IAM role の定義 (Python 版 `pocket_cli/resources/aws/iam_roles.py`)。
//!
//! pocket が作る role は 4 種で、どれも [`RoleSpec`] で表す。
//!
//! - CloudFormation 経路: Lambda 実行 role / scheduler role。container stack の template が
//!   `cfn_properties` をそのまま埋め込む
//! - API 経路: CodeBuild role / AWS Backup role (`deploy::iam` の `ensure_role` で作る)
//!
//! `[iam] external_roles = true` では pocket は role を作らず、[`stage_role_specs`] の
//! 一覧を deploy の前に検査する。

// API 経路の role と stage 全体の role 一覧は deploy の移植で使う (今は template の描画と
// `build` だけが使う)。deploy が入ったら外す
#![allow(dead_code)]

use anyhow::{bail, Result};
use magic_pocket_settings::context::{
    ContainerContext, Context, DsqlContext, InboundContext, RdsContext, SchedulerContext,
};
use magic_pocket_settings::model::{BuildBackend, PasswordStrategy, StoreType};
use serde_json::{json, Map, Value as Json};

use crate::image_build::codebuild::Names as CodeBuildNames;

const POLICY_VERSION: &str = "2012-10-17";

#[derive(Debug, Clone, PartialEq)]
pub struct RoleSpec {
    pub name: String,
    pub service: String,
    pub managed_policy_arns: Vec<String>,
    /// PolicyName → PolicyDocument (挿入順)
    pub inline_policies: Map<String, Json>,
    pub permissions_boundary: Option<String>,
    /// 信頼ポリシーに Version を書くか (既存 stack の文書と一致させる role だけ false)
    pub assume_role_policy_version: bool,
    /// `[iam] external_roles`。pocket は作らず、利用者が事前に作った role を使う
    pub external: bool,
}

impl RoleSpec {
    /// template からこの role の ARN を参照する式
    pub fn cfn_arn(&self, logical_id: &str) -> Json {
        if self.external {
            return sub(format!(
                "arn:${{AWS::Partition}}:iam::${{AWS::AccountId}}:role/{}",
                self.name
            ));
        }
        json!({"Fn::GetAtt": format!("{logical_id}.Arn")})
    }

    pub fn assume_role_policy(&self) -> Json {
        let mut document = Map::new();
        if self.assume_role_policy_version {
            document.insert("Version".into(), json!(POLICY_VERSION));
        }
        document.insert(
            "Statement".into(),
            json!([{
                "Effect": "Allow",
                "Principal": {"Service": self.service},
                "Action": "sts:AssumeRole",
            }]),
        );
        Json::Object(document)
    }

    /// `AWS::IAM::Role` の Properties
    pub fn cfn_properties(&self) -> Json {
        let mut props = Map::new();
        props.insert("RoleName".into(), json!(self.name));
        props.insert("AssumeRolePolicyDocument".into(), self.assume_role_policy());
        if !self.managed_policy_arns.is_empty() {
            props.insert("ManagedPolicyArns".into(), json!(self.managed_policy_arns));
        }
        if !self.inline_policies.is_empty() {
            let policies: Vec<Json> = self
                .inline_policies
                .iter()
                .map(|(name, doc)| json!({"PolicyName": name, "PolicyDocument": doc}))
                .collect();
            props.insert("Policies".into(), Json::Array(policies));
        }
        if let Some(boundary) = self
            .permissions_boundary
            .as_deref()
            .filter(|b| !b.is_empty())
        {
            props.insert("PermissionsBoundary".into(), json!(boundary));
        }
        Json::Object(props)
    }

    /// `Fn::Sub` を展開した inline policy (IAM に直接書ける形)
    pub fn resolved_inline_policies(
        &self,
        region: &str,
        account_id: &str,
    ) -> Result<Map<String, Json>> {
        self.inline_policies
            .iter()
            .map(|(name, doc)| Ok((name.clone(), resolve_intrinsics(doc, region, account_id)?)))
            .collect()
    }

    /// template に渡す形 (external かどうか / Properties / ARN の参照式)
    pub fn template_input(&self, logical_id: &str) -> Json {
        json!({
            "external": self.external,
            "cfn_properties": self.cfn_properties(),
            "arn": self.cfn_arn(logical_id),
        })
    }
}

fn policy(statements: Vec<Json>) -> Json {
    json!({"Version": POLICY_VERSION, "Statement": statements})
}

fn allow(actions: &[&str], resources: Vec<Json>) -> Json {
    json!({"Effect": "Allow", "Action": actions, "Resource": resources})
}

fn sub(value: impl Into<String>) -> Json {
    json!({"Fn::Sub": value.into()})
}

/// `allow` に Condition を足した statement
fn allow_if(actions: &[&str], resources: Vec<Json>, condition: Json) -> Json {
    let mut statement = allow(actions, resources);
    statement["Condition"] = condition;
    statement
}

/// container の Lambda 実行 role (全 handler 共通)
pub fn lambda_role(
    ctx: &ContainerContext,
    rds: Option<&RdsContext>,
    dsql: Option<&DsqlContext>,
) -> RoleSpec {
    let prefix = &ctx.resource_prefix;
    let mut policies = Map::new();
    for inlet in ctx.inbound.values() {
        policies.insert(format!("inbound-{}", inlet.name), inbound_policy(inlet));
    }
    policies.extend(secrets_policies(ctx));
    if let Some(rds) = rds {
        policies.extend(rds_policies(prefix, rds));
    }
    if let Some(dsql) = dsql {
        policies.insert(format!("{prefix}access-dsql"), dsql_policy(dsql));
    }
    policies.insert(
        format!("{prefix}access-cloudformation"),
        policy(vec![allow(
            &["cloudformation:DescribeStacks"],
            vec![sub(format!(
                "arn:aws:cloudformation:${{AWS::Region}}:${{AWS::AccountId}}:stack/{}-*",
                ctx.slug
            ))],
        )]),
    );
    for (name, doc) in ctx.iam.inline_policies.iter() {
        let doc = serde_json::to_value(doc).expect("inline policy は JSON にできる");
        policies.insert(format!("{prefix}{name}"), doc);
    }

    if ctx.external_roles {
        return RoleSpec {
            name: format!("{}lambda-role", ctx.external_role_prefix),
            service: "lambda.amazonaws.com".into(),
            managed_policy_arns: lambda_managed_policies(ctx),
            inline_policies: policies,
            permissions_boundary: None,
            assume_role_policy_version: true,
            external: true,
        };
    }
    RoleSpec {
        name: format!("lambda-{}-{}-{}", ctx.slug, ctx.name, ctx.namespace),
        service: "lambda.amazonaws.com".into(),
        managed_policy_arns: lambda_managed_policies(ctx),
        inline_policies: policies,
        permissions_boundary: ctx.permissions_boundary.clone(),
        // 0.38.0 以前の container stack は Version なしで LambdaRole を作っている
        assume_role_policy_version: false,
        external: false,
    }
}

fn lambda_managed_policies(ctx: &ContainerContext) -> Vec<String> {
    let mut managed =
        vec!["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole".to_string()];
    if ctx.vpc.is_some() {
        managed.push(
            "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole".to_string(),
        );
    }
    for (enabled, policy_name) in [
        (ctx.use_ses, "AmazonSESFullAccess"),
        (ctx.use_s3, "AmazonS3FullAccess"),
        (ctx.use_route53, "AmazonRoute53FullAccess"),
        (ctx.use_sqs, "AmazonSQSFullAccess"),
        (ctx.use_efs, "AmazonElasticFileSystemFullAccess"),
    ] {
        if enabled {
            managed.push(format!("arn:aws:iam::aws:policy/{policy_name}"));
        }
    }
    managed.extend(ctx.iam.managed_policy_arns.iter().cloned());
    managed
}

/// 受信口の bucket を読む権限。受信した原本と metadata は消させない
fn inbound_policy(inlet: &InboundContext) -> Json {
    let bucket = format!("arn:${{AWS::Partition}}:s3:::{}", inlet.bucket_name);
    let raw = sub(format!("{bucket}/{}*", inlet.config.raw_prefix));
    let metadata = sub(format!("{bucket}/{}*", inlet.config.metadata_prefix));
    let imported = sub(format!("{bucket}/{}*", inlet.config.import_prefix));
    policy(vec![
        allow(&["s3:ListBucket"], vec![sub(bucket)]),
        json!({
            "Effect": "Deny",
            "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"],
            "Resource": [raw, metadata],
        }),
        allow(
            &["s3:GetObject", "s3:GetObjectVersion"],
            vec![raw.clone(), imported, metadata.clone()],
        ),
        allow(&["s3:PutObject"], vec![metadata]),
    ])
}

fn secrets_policies(ctx: &ContainerContext) -> Map<String, Json> {
    let prefix = &ctx.resource_prefix;
    let mut policies = Map::new();
    if !ctx.allowed_sm_resources.is_empty() {
        let mut statements = vec![allow(
            &["secretsmanager:GetSecretValue"],
            ctx.allowed_sm_resources.iter().map(sub).collect(),
        )];
        if ctx.require_list_secrets {
            statements.push(allow(&["secretsmanager:ListSecrets"], vec![json!("*")]));
        }
        policies.insert(format!("{prefix}access-secretsmanager"), policy(statements));
    }
    if !ctx.allowed_ssm_resources.is_empty() {
        policies.insert(
            format!("{prefix}access-ssm"),
            policy(vec![allow(
                &[
                    "ssm:GetParameter",
                    "ssm:GetParameters",
                    "ssm:GetParametersByPath",
                ],
                ctx.allowed_ssm_resources.iter().map(sub).collect(),
            )]),
        );
    }
    policies
}

/// RDS の認証情報を読む権限
fn rds_policies(prefix: &str, rds: &RdsContext) -> Map<String, Json> {
    let mut policies = Map::new();
    let secret_key = format!("{prefix}access-rds-secret");
    if !rds.managed {
        if let Some(arn) = rds.secret_arn.as_deref().filter(|a| !a.is_empty()) {
            policies.insert(
                secret_key,
                policy(vec![allow(
                    &["secretsmanager:GetSecretValue"],
                    vec![json!(arn)],
                )]),
            );
        }
        return policies;
    }
    if rds.password_strategy == PasswordStrategy::Static {
        let name = &rds.credentials_secret_name;
        if rds.secret_store == StoreType::Ssm {
            policies.insert(
                format!("{prefix}access-rds-ssm"),
                policy(vec![allow(
                    &["ssm:GetParameter"],
                    vec![sub(format!(
                        "arn:aws:ssm:${{AWS::Region}}:${{AWS::AccountId}}:parameter/{name}"
                    ))],
                )]),
            );
            return policies;
        }
        // Secrets Manager は secret 名の後ろに "-" + 6 文字の乱数を付けて ARN にする
        let resource = sub(format!(
            "arn:${{AWS::Partition}}:secretsmanager:{}:${{AWS::AccountId}}:secret:{name}-??????",
            rds.region
        ));
        policies.insert(
            secret_key,
            policy(vec![allow(
                &["secretsmanager:GetSecretValue"],
                vec![resource],
            )]),
        );
        return policies;
    }
    // aws-managed: RDS が作る secret (rds!cluster-<uuid>) には元の cluster の ARN が
    // aws: タグで付く
    let resource = sub(format!(
        "arn:${{AWS::Partition}}:secretsmanager:{}:${{AWS::AccountId}}:secret:rds!cluster-*",
        rds.region
    ));
    let cluster_arn = sub(format!(
        "arn:${{AWS::Partition}}:rds:{}:${{AWS::AccountId}}:cluster:{}",
        rds.region, rds.cluster_identifier
    ));
    policies.insert(
        secret_key,
        policy(vec![allow_if(
            &["secretsmanager:GetSecretValue"],
            vec![resource],
            json!({
                "StringEquals": {
                    "aws:ResourceTag/aws:rds:primaryDBClusterArn": cluster_arn
                }
            }),
        )]),
    );
    policies
}

/// DSQL に admin で接続する権限。cluster は pocket が付ける Name タグで絞る
fn dsql_policy(dsql: &DsqlContext) -> Json {
    let resource = sub(format!(
        "arn:${{AWS::Partition}}:dsql:{}:${{AWS::AccountId}}:cluster/*",
        dsql.region
    ));
    policy(vec![allow_if(
        &["dsql:DbConnectAdmin"],
        vec![resource],
        json!({"StringEquals": {"aws:ResourceTag/Name": dsql.tag_name}}),
    )])
}

/// EventBridge Scheduler が handler を起動する role (container 単位)
pub fn scheduler_role(
    scheduler: &SchedulerContext,
    permissions_boundary: Option<&str>,
    external: bool,
) -> RoleSpec {
    let mut statements = Vec::new();
    if !scheduler.invoked_function_arns.is_empty() {
        statements.push(allow(
            &["lambda:InvokeFunction"],
            scheduler.invoked_function_arns.iter().map(sub).collect(),
        ));
    }
    if !scheduler.sqs_queue_arns.is_empty() {
        statements.push(allow(
            &["sqs:SendMessage"],
            scheduler.sqs_queue_arns.iter().map(sub).collect(),
        ));
    }
    let mut inline_policies = Map::new();
    inline_policies.insert("invoke-handlers".into(), policy(statements));
    RoleSpec {
        name: scheduler.role_name.clone(),
        service: "scheduler.amazonaws.com".into(),
        managed_policy_arns: Vec::new(),
        inline_policies,
        permissions_boundary: permissions_boundary.map(str::to_string),
        assume_role_policy_version: true,
        external,
    }
}

// --- API 経路 ---

const BACKUP_ROLE_POLICIES: [&str; 2] = [
    "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup",
    "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForRestores",
];

pub struct CodeBuildRole<'a> {
    pub name: &'a str,
    pub region: &'a str,
    pub account_id: &'a str,
    pub state_bucket: &'a str,
    pub project_name: &'a str,
    pub permissions_boundary: Option<&'a str>,
    pub external: bool,
}

/// CodeBuild で image を build して ECR へ push する role
pub fn codebuild_role(role: &CodeBuildRole) -> RoleSpec {
    let CodeBuildRole {
        region,
        account_id,
        state_bucket,
        project_name,
        ..
    } = role;
    let document = policy(vec![
        json!({
            "Effect": "Allow",
            "Action": ["ecr:GetAuthorizationToken"],
            "Resource": "*",
        }),
        json!({
            "Effect": "Allow",
            "Action": [
                "ecr:BatchCheckLayerAvailability",
                "ecr:GetDownloadUrlForLayer",
                "ecr:BatchGetImage",
                "ecr:PutImage",
                "ecr:InitiateLayerUpload",
                "ecr:UploadLayerPart",
                "ecr:CompleteLayerUpload",
            ],
            "Resource": format!("arn:aws:ecr:{region}:{account_id}:repository/*"),
        }),
        json!({
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:GetObjectVersion"],
            "Resource": format!("arn:aws:s3:::{state_bucket}/codebuild/*"),
        }),
        json!({
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
            ],
            "Resource": format!(
                "arn:aws:logs:{region}:{account_id}:log-group:/aws/codebuild/{project_name}*"
            ),
        }),
    ]);
    let mut inline_policies = Map::new();
    inline_policies.insert("codebuild-policy".into(), document);
    RoleSpec {
        name: role.name.to_string(),
        service: "codebuild.amazonaws.com".into(),
        managed_policy_arns: Vec::new(),
        inline_policies,
        permissions_boundary: role.permissions_boundary.map(str::to_string),
        assume_role_policy_version: true,
        external: role.external,
    }
}

/// AWS Backup のサービスロール (stage 単位)。AWSBackupDefaultServiceRole は console の
/// 初回操作で作られるもので、API しか使わないアカウントには無いため pocket が作る
pub fn backup_role(name: &str, permissions_boundary: Option<&str>, external: bool) -> RoleSpec {
    RoleSpec {
        name: name.to_string(),
        service: "backup.amazonaws.com".into(),
        managed_policy_arns: BACKUP_ROLE_POLICIES.iter().map(|p| p.to_string()).collect(),
        inline_policies: Map::new(),
        permissions_boundary: permissions_boundary.map(str::to_string),
        assume_role_policy_version: true,
        external,
    }
}

// --- external_roles ---

/// stage の deploy / backup で使う role を定義順 (container → CodeBuild → AWS Backup) で
/// 返す (Python の `stage_role_specs`)。AWS への問い合わせはしない。
///
/// external_roles では role は種別単位なので、container ごとの Lambda / scheduler の定義を
/// 同名で 1 つにまとめる (権限は和集合)。`codebuild_boundary_env` は
/// `CODEBUILD_PERMISSIONS_BOUNDARY` の値
pub fn stage_role_specs(
    ctx: &Context,
    account_id: &str,
    codebuild_boundary_env: Option<&str>,
) -> Result<Vec<RoleSpec>> {
    let mut specs = Vec::new();
    let mut names: Vec<&String> = ctx.container.keys().collect();
    names.sort();
    for name in names {
        let c = ctx.container.get(name).expect("container");
        specs.push(super::container::lambda_role(
            c,
            ctx.rds.as_ref(),
            ctx.dsql.as_ref(),
        ));
        specs.extend(super::container::scheduler_role(c, ctx.scheduler.get(name)));
    }
    specs.extend(stage_codebuild_role(
        ctx,
        account_id,
        codebuild_boundary_env,
    ));
    specs.extend(stage_backup_role(ctx));
    merge_role_specs(specs)
}

/// CodeBuild project / role は stage で 1 つ (container 間で共有)
fn stage_codebuild_role(
    ctx: &Context,
    account_id: &str,
    boundary_env: Option<&str>,
) -> Option<RoleSpec> {
    let general = ctx.general.as_ref()?;
    let first = ctx
        .container
        .values()
        .find(|c| c.build.backend == BuildBackend::Codebuild)?;
    let names = CodeBuildNames::new(
        &first.resource_prefix,
        ctx.external_roles,
        &ctx.external_role_prefix,
    );
    let boundary = boundary_env
        .filter(|v| !v.is_empty())
        .or(first.permissions_boundary.as_deref());
    Some(codebuild_role(&CodeBuildRole {
        name: &names.role,
        region: &general.region,
        account_id,
        state_bucket: &state_bucket_name(ctx)?,
        project_name: &names.project,
        permissions_boundary: boundary,
        external: ctx.external_roles,
    }))
}

/// state bucket の名前 (Python の `create_state_store(context).bucket_name`)
pub fn state_bucket_name(ctx: &Context) -> Option<String> {
    let general = ctx.general.as_ref()?;
    let prefix = general
        .prefix_template
        .replace("{stage}", &ctx.stage)
        .replace("{project}", &ctx.project_name)
        .replace("{namespace}", &general.namespace);
    Some(format!("{prefix}state"))
}

/// AWS Backup のサービスロール。dsql はオンデマンドバックアップ / restore で `[backup]` の
/// 宣言が無くても使う。rds は `[backup.rds]` を宣言したときだけ
fn stage_backup_role(ctx: &Context) -> Option<RoleSpec> {
    if let Some(dsql) = &ctx.dsql {
        return Some(backup_role(
            &dsql.backup_role_name,
            dsql.permissions_boundary.as_deref(),
            ctx.external_roles,
        ));
    }
    let backup = ctx.backup.as_ref().filter(|b| !b.plans.is_empty())?;
    Some(backup_role(
        &backup.role_name,
        backup.permissions_boundary.as_deref(),
        ctx.external_roles,
    ))
}

/// 同名の RoleSpec を 1 つにまとめる (external_roles の種別単位 role 向け)。
///
/// managed policy は和集合、同名の inline policy は Statement の和集合にする (共用 role は
/// 全 container の権限を必要とする)。順序は初出順を保つ
pub fn merge_role_specs(specs: Vec<RoleSpec>) -> Result<Vec<RoleSpec>> {
    let mut merged: Vec<RoleSpec> = Vec::new();
    for spec in specs {
        let Some(base) = merged.iter_mut().find(|m| m.name == spec.name) else {
            merged.push(spec);
            continue;
        };
        if base.service != spec.service {
            bail!(
                "role {} を {} と {} の両方が使おうとしています",
                spec.name,
                base.service,
                spec.service
            );
        }
        for arn in spec.managed_policy_arns {
            if !base.managed_policy_arns.contains(&arn) {
                base.managed_policy_arns.push(arn);
            }
        }
        for (name, doc) in spec.inline_policies {
            let doc = match base.inline_policies.get(&name) {
                Some(existing) => merge_policy(existing, &doc),
                None => doc,
            };
            base.inline_policies.insert(name, doc);
        }
    }
    Ok(merged)
}

/// 2 つの policy 文書の Statement を重複なしで連結する
fn merge_policy(a: &Json, b: &Json) -> Json {
    let mut statements: Vec<Json> = Vec::new();
    for doc in [a, b] {
        let raw: Vec<Json> = match doc.get("Statement") {
            Some(Json::Array(items)) => items.clone(),
            Some(item @ Json::Object(_)) => vec![item.clone()],
            _ => Vec::new(),
        };
        for statement in raw {
            // serde_json の Map の比較は key の順によらない (Python は sort_keys で比べる)
            if !statements.contains(&statement) {
                statements.push(statement);
            }
        }
    }
    let version = a.get("Version").cloned().unwrap_or(json!(POLICY_VERSION));
    json!({"Version": version, "Statement": statements})
}

/// policy 文書中の `Fn::Sub` を展開する。他の組み込み関数は扱わない
pub fn resolve_intrinsics(value: &Json, region: &str, account_id: &str) -> Result<Json> {
    match value {
        Json::Object(map) => {
            if map.len() == 1 {
                if let Some(Json::String(template)) = map.get("Fn::Sub") {
                    return Ok(Json::String(substitute(template, region, account_id)?));
                }
            }
            map.iter()
                .map(|(k, v)| Ok((k.clone(), resolve_intrinsics(v, region, account_id)?)))
                .collect::<Result<Map<String, Json>>>()
                .map(Json::Object)
        }
        Json::Array(items) => items
            .iter()
            .map(|v| resolve_intrinsics(v, region, account_id))
            .collect::<Result<Vec<Json>>>()
            .map(Json::Array),
        other => Ok(other.clone()),
    }
}

/// `${AWS::Region}` 等の擬似パラメータを値に置き換える
fn substitute(template: &str, region: &str, account_id: &str) -> Result<String> {
    let mut out = String::with_capacity(template.len());
    let mut rest = template;
    while let Some(start) = rest.find("${") {
        // Python の正規表現 `\$\{([^}]+)\}` と同じく、閉じていない・空の `${}` は残す
        let Some(len) = rest[start + 2..].find('}').filter(|len| *len > 0) else {
            out.push_str(&rest[..start + 2]);
            rest = &rest[start + 2..];
            continue;
        };
        out.push_str(&rest[..start]);
        let name = &rest[start + 2..start + 2 + len];
        out.push_str(match name {
            "AWS::Region" => region,
            "AWS::AccountId" => account_id,
            "AWS::Partition" => "aws",
            _ => bail!("展開できない Fn::Sub の変数です: ${{{name}}}"),
        });
        rest = &rest[start + 2 + len + 1..];
    }
    out.push_str(rest);
    Ok(out)
}

/// 信頼ポリシーが service に sts:AssumeRole を許可しているか (external role の検査)
pub fn trusts_service(document: &Json, service: &str) -> bool {
    let statements: Vec<&Json> = match document.get("Statement") {
        Some(Json::Array(items)) => items.iter().collect(),
        Some(item @ Json::Object(_)) => vec![item],
        _ => vec![],
    };
    let as_list = |v: Option<&Json>| -> Vec<String> {
        match v {
            Some(Json::String(s)) => vec![s.clone()],
            Some(Json::Array(items)) => items
                .iter()
                .filter_map(|i| i.as_str().map(str::to_string))
                .collect(),
            _ => vec![],
        }
    };
    statements.into_iter().any(|statement| {
        if statement.get("Effect").and_then(Json::as_str) != Some("Allow") {
            return false;
        }
        let services = as_list(statement.get("Principal").and_then(|p| p.get("Service")));
        let actions = as_list(statement.get("Action"));
        services.iter().any(|s| s == service) && actions.iter().any(|a| a == "sts:AssumeRole")
    })
}

#[cfg(test)]
mod tests {
    use super::super::testing::{read_json, repo_root, vector_dirs};
    use super::*;

    fn spec_json(spec: &RoleSpec, region: &str, account_id: &str) -> Json {
        json!({
            "name": spec.name,
            "service": spec.service,
            "managed_policy_arns": spec.managed_policy_arns,
            "inline_policies": spec.inline_policies,
            "permissions_boundary": spec.permissions_boundary,
            "assume_role_policy_version": spec.assume_role_policy_version,
            "external": spec.external,
            "assume_role_policy": spec.assume_role_policy(),
            "resolved_inline_policies":
                spec.resolved_inline_policies(region, account_id).expect("resolve"),
        })
    }

    /// 文書の key の順まで含めて比べる (IAM に書く JSON のテキストを Python と揃える)
    fn dump(value: &Json) -> String {
        serde_json::to_string_pretty(value).expect("JSON")
    }

    #[test]
    fn test_stage_roles_match_python() {
        let root = repo_root();
        let mut failures = Vec::new();
        let mut count = 0;
        let mut merged = 0;
        for dir in vector_dirs() {
            let manifest = read_json(&dir.join("manifest.json"));
            let want = read_json(&dir.join("roles.json"));
            let ctx = super::super::testing::context(&root, &manifest);
            let region = ctx
                .general
                .as_ref()
                .map(|g| g.region.as_str())
                .unwrap_or("");
            let account_id = "123456789012";
            let specs = stage_role_specs(&ctx, account_id, None).expect("stage_role_specs");
            let got = Json::Array(
                specs
                    .iter()
                    .map(|s| spec_json(s, region, account_id))
                    .collect(),
            );
            count += specs.len();
            let containers = ctx.container.len();
            if ctx.external_roles && containers > 1 {
                merged += 1;
            }
            if dump(&got) != dump(&want) {
                failures.push(format!(
                    "{}:\n  rust:   {got}\n  python: {want}",
                    dir.display()
                ));
            }
        }
        assert!(failures.is_empty(), "{}", failures.join("\n"));
        assert!(count > 80, "role が少なすぎます: {count}");
        assert!(
            merged > 0,
            "external_roles で複数 container の vector がありません"
        );
    }

    fn role(name: &str, service: &str, managed: &[&str], inline: Json) -> RoleSpec {
        RoleSpec {
            name: name.into(),
            service: service.into(),
            managed_policy_arns: managed.iter().map(|m| m.to_string()).collect(),
            inline_policies: inline.as_object().cloned().unwrap_or_default(),
            permissions_boundary: None,
            assume_role_policy_version: true,
            external: true,
        }
    }

    #[test]
    fn test_merge_unions_policies_in_first_seen_order() {
        let allow_a = json!({"Effect": "Allow", "Action": ["a"], "Resource": ["x"]});
        // key の順が違うだけの statement は同じものとして扱う
        let allow_a_reordered = json!({"Resource": ["x"], "Action": ["a"], "Effect": "Allow"});
        let allow_b = json!({"Effect": "Allow", "Action": ["b"], "Resource": ["y"]});
        let merged = merge_role_specs(vec![
            role(
                "shared",
                "lambda.amazonaws.com",
                &["m1", "m2"],
                json!({"p": {"Version": "2012-10-17", "Statement": [allow_a]}}),
            ),
            role("other", "scheduler.amazonaws.com", &[], json!({})),
            role(
                "shared",
                "lambda.amazonaws.com",
                &["m2", "m3"],
                json!({
                    "p": {"Version": "2012-10-17", "Statement": [allow_a_reordered, allow_b]},
                    "q": {"Version": "2012-10-17", "Statement": allow_b},
                }),
            ),
        ])
        .unwrap();
        assert_eq!(merged.len(), 2);
        assert_eq!(merged[0].name, "shared");
        assert_eq!(merged[0].managed_policy_arns, ["m1", "m2", "m3"]);
        assert_eq!(
            merged[0].inline_policies["p"]["Statement"],
            json!([allow_a, allow_b])
        );
        assert_eq!(merged[0].inline_policies["q"]["Statement"], allow_b);
        assert_eq!(merged[1].name, "other");
    }

    #[test]
    fn test_merge_rejects_one_name_for_two_services() {
        let error = merge_role_specs(vec![
            role("shared", "lambda.amazonaws.com", &[], json!({})),
            role("shared", "scheduler.amazonaws.com", &[], json!({})),
        ])
        .unwrap_err();
        assert_eq!(
            error.to_string(),
            "role shared を lambda.amazonaws.com と scheduler.amazonaws.com の両方が使おうとしています"
        );
    }

    #[test]
    fn test_resolve_intrinsics() {
        let doc = json!({
            "Resource": [
                {"Fn::Sub": "arn:${AWS::Partition}:s3:${AWS::Region}:${AWS::AccountId}:x"},
                "plain-${AWS::Region}",
                {"Fn::Sub": "a", "other": 1},
            ]
        });
        assert_eq!(
            resolve_intrinsics(&doc, "ap-northeast-1", "123").unwrap(),
            json!({
                "Resource": [
                    "arn:aws:s3:ap-northeast-1:123:x",
                    "plain-${AWS::Region}",
                    {"Fn::Sub": "a", "other": 1},
                ]
            })
        );
        let error = resolve_intrinsics(&json!({"Fn::Sub": "${Stage}"}), "r", "1").unwrap_err();
        assert_eq!(
            error.to_string(),
            "展開できない Fn::Sub の変数です: ${Stage}"
        );
        assert_eq!(
            resolve_intrinsics(&json!({"Fn::Sub": "a${}b${open"}), "r", "1").unwrap(),
            json!("a${}b${open")
        );
    }

    #[test]
    fn test_trusts_service() {
        let service = "codebuild.amazonaws.com";
        let spec = backup_role("r", None, false);
        assert!(trusts_service(
            &spec.assume_role_policy(),
            "backup.amazonaws.com"
        ));
        assert!(!trusts_service(&spec.assume_role_policy(), service));
        let single = json!({"Statement": {"Effect": "Allow", "Principal": {"Service": ["x", service]}, "Action": ["sts:AssumeRole"]}});
        assert!(trusts_service(&single, service));
        let deny = json!({"Statement": [{"Effect": "Deny", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}]});
        assert!(!trusts_service(&deny, service));
        let star = json!({"Statement": [{"Effect": "Allow", "Principal": "*", "Action": "sts:AssumeRole"}]});
        assert!(!trusts_service(&star, service));
    }
}
