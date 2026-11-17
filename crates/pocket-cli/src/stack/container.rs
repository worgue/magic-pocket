//! container (Lambda + API Gateway + SQS + scheduler) の stack (Python 版 `ContainerStack`)。

use anyhow::{bail, Result};
use magic_pocket_settings::context::{ContainerContext, DsqlContext, RdsContext, SchedulerContext};
use magic_pocket_settings::model::{PasswordStrategy, StoreType};
use serde_json::{json, Map, Value as Json};

use super::render::render_template;
use super::roles::{self, RoleSpec};
use super::{dump, variables, vpc, StackOutputs, StackTemplate};

pub fn stack_name(ctx: &ContainerContext) -> String {
    format!("{}-container-{}", ctx.slug, ctx.name)
}

/// pocket が管理する RDS (`[rds]` の managed = true) の実体から引く値
#[derive(Debug, Clone, Default)]
pub struct RdsState {
    /// RDS 用 security group (Name タグで探す) の id
    pub security_group_id: Option<String>,
    /// cluster の Endpoint
    pub endpoint: Option<String>,
    /// cluster の Port
    pub port: Option<i64>,
    /// cluster の MasterUserSecret.SecretArn (aws-managed のとき RDS が作る)
    pub master_user_secret_arn: Option<String>,
    /// password_strategy = "static" で pocket が作った secret の ARN
    pub static_secret_arn: Option<String>,
}

/// container stack が外から引く値
#[derive(Debug, Clone, Default)]
pub struct ContainerInputs {
    /// `[rds]` が managed のときの実体 (未作成なら各値が `None`)
    pub rds: RdsState,
    /// DSQL cluster の identifier (未作成なら `None`)
    pub dsql_identifier: Option<String>,
    /// 外部 VPC (manage = false) の VPC stack の output。zone の数を数える
    pub vpc_output: Option<StackOutputs>,
}

/// container stack。`rds` / `dsql` / `scheduler` は Context の該当部分
pub fn container_stack(
    ctx: &ContainerContext,
    rds: Option<&RdsContext>,
    dsql: Option<&DsqlContext>,
    scheduler: Option<&SchedulerContext>,
    inputs: &ContainerInputs,
) -> Result<StackTemplate> {
    // VPC 外 (use_vpc=false) の container は private subnet の RDS に到達できないので、
    // [rds] を無いものとして扱う (POCKET_RDS_* env・secret の IAM policy・SG への
    // ingress を出さない)
    let rds = if ctx.vpc.is_some() { rds } else { None };
    let name = stack_name(ctx);
    let export = match &ctx.vpc {
        Some(v) => vpc::export(v),
        None => Map::new(),
    };

    let mut context = dump(ctx);
    // 外部 VPC: zones は VPC stack の output から数える
    if let Some(v) = ctx.vpc.as_ref().filter(|v| !v.manage) {
        let count = vpc_zone_count(v, &export, inputs)?;
        let zones: Vec<String> = (0..count)
            .map(|i| format!("{}{}", v.region, (b'a' + i as u8) as char))
            .collect();
        context["vpc"]["zones"] = json!(zones);
    }

    let mut extra = rds.map(|r| resolve_rds(r, &inputs.rds)).unwrap_or_default();
    let use_rds = !extra.is_empty();
    for key in RDS_KEYS {
        extra.entry(key).or_insert(Json::Null);
    }
    extra.insert("use_rds".into(), json!(use_rds));
    let dsql_endpoint = dsql.and_then(|d| {
        inputs
            .dsql_identifier
            .as_deref()
            .filter(|id| !id.is_empty())
            .map(|id| format!("{id}.dsql.{}.on.aws", d.region))
    });
    extra.insert("use_dsql".into(), json!(dsql_endpoint.is_some()));
    extra.insert("dsql_endpoint".into(), json!(dsql_endpoint));
    extra.insert("dsql_region".into(), json!(dsql.map(|d| &d.region)));
    extra.insert("scheduler".into(), json!(scheduler.map(dump)));
    extra.insert(
        "lambda_role".into(),
        lambda_role(ctx, rds, dsql).template_input("LambdaRole"),
    );
    extra.insert(
        "scheduler_role".into(),
        json!(scheduler_role(ctx, scheduler).map(|r| r.template_input("SchedulerExecutionRole"))),
    );

    let vars = variables(&name, &export, extra, context);
    Ok(StackTemplate {
        body: render_template("container", &vars)?,
        name,
        region: ctx.region.clone(),
        capabilities: vec!["CAPABILITY_NAMED_IAM".to_string()],
        tags: Vec::new(),
        export,
    })
}

/// container の Lambda 実行 role (VPC 外の container は [rds] を無いものとして扱う)
pub fn lambda_role(
    ctx: &ContainerContext,
    rds: Option<&RdsContext>,
    dsql: Option<&DsqlContext>,
) -> RoleSpec {
    let rds = if ctx.vpc.is_some() { rds } else { None };
    roles::lambda_role(ctx, rds, dsql)
}

/// scheduler の role (schedule が無ければ `None`)
pub fn scheduler_role(
    ctx: &ContainerContext,
    scheduler: Option<&SchedulerContext>,
) -> Option<RoleSpec> {
    let scheduler = scheduler.filter(|s| s.has_schedules)?;
    Some(roles::scheduler_role(
        scheduler,
        ctx.permissions_boundary.as_deref(),
        ctx.external_roles,
    ))
}

const RDS_KEYS: [&str; 7] = [
    "rds_security_group_id",
    "rds_secret_store",
    "rds_secret_arn",
    "rds_ssm_param_name",
    "rds_endpoint",
    "rds_port",
    "rds_dbname",
];

/// RDS の接続情報 (template の `rds_*` 変数)
fn resolve_rds(rds: &RdsContext, state: &RdsState) -> Map<String, Json> {
    let mut info = Map::new();
    // managed = false: 接続情報は Context に直接入っている
    if !rds.managed {
        info.insert("rds_security_group_id".into(), json!(rds.security_group_id));
        info.insert("rds_secret_store".into(), json!("sm"));
        info.insert("rds_secret_arn".into(), json!(rds.secret_arn));
        info.insert("rds_ssm_param_name".into(), Json::Null);
        info.insert("rds_endpoint".into(), Json::Null);
        info.insert("rds_port".into(), Json::Null);
        info.insert("rds_dbname".into(), Json::Null);
        return info;
    }
    info.insert(
        "rds_security_group_id".into(),
        json!(state.security_group_id),
    );
    info.insert("rds_endpoint".into(), json!(state.endpoint));
    // Python: `str(port) if port else None`
    let port = state.port.filter(|p| *p != 0).map(|p| p.to_string());
    info.insert("rds_port".into(), json!(port));
    info.insert("rds_dbname".into(), json!(rds.database_name));
    let is_static = rds.password_strategy == PasswordStrategy::Static;
    // static + secret_store=ssm: SSM パラメータを参照する (SM secret は作らない)
    if is_static && rds.secret_store == StoreType::Ssm {
        info.insert("rds_secret_store".into(), json!("ssm"));
        info.insert("rds_secret_arn".into(), Json::Null);
        info.insert(
            "rds_ssm_param_name".into(),
            json!(rds.credentials_secret_name),
        );
        return info;
    }
    let secret_arn = if is_static {
        &state.static_secret_arn
    } else {
        &state.master_user_secret_arn
    };
    info.insert("rds_secret_store".into(), json!("sm"));
    info.insert("rds_secret_arn".into(), json!(secret_arn));
    info.insert("rds_ssm_param_name".into(), Json::Null);
    info
}

/// 外部 VPC の private subnet の数 (VPC stack の Export `<prefix>1`, `<prefix>2`, ... の連番)
fn vpc_zone_count(
    v: &magic_pocket_settings::context::VpcContext,
    export: &Map<String, Json>,
    inputs: &ContainerInputs,
) -> Result<usize> {
    let Some(output) = inputs.vpc_output.as_ref().filter(|o| !o.is_empty()) else {
        bail!(
            "VPC stack '{}' の output が取得できません",
            vpc::stack_name(v)
        );
    };
    let prefix = export["private_subnet_"].as_str().unwrap_or_default();
    Ok((1..20)
        .take_while(|i| output.contains_key(&format!("{prefix}{i}")))
        .count())
}
