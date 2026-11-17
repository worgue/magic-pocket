//! pocket が作る IAM role の定義 (Python 版 `pocket_cli/resources/aws/iam_roles.py`)。
//!
//! ここにあるのは CloudFormation 経路の 2 種 (Lambda 実行 role / scheduler role)。
//! container stack の template が `cfn_properties` をそのまま埋め込む。

use magic_pocket_settings::context::{
    ContainerContext, DsqlContext, InboundContext, RdsContext, SchedulerContext,
};
use magic_pocket_settings::model::{PasswordStrategy, StoreType};
use serde_json::{json, Map, Value as Json};

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
