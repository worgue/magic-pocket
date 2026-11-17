//! VPC の stack (Python 版 `VpcStack`)。

use anyhow::Result;
use magic_pocket_settings::context::VpcContext;
use serde_json::{json, Map, Value as Json};

use super::render::render_template;
use super::{dump, variables, StackTemplate};

pub fn stack_name(vpc: &VpcContext) -> String {
    format!("{}-vpc", vpc.name)
}

/// VPC stack が公開する Export 名 (container stack が ImportValue で参照する)
pub fn export(vpc: &VpcContext) -> Map<String, Json> {
    let mut export = Map::new();
    export.insert("vpc_id".into(), json!(format!("{}-vpc-id", vpc.name)));
    export.insert(
        "private_subnet_".into(),
        json!(format!("{}-private-subnet-", vpc.name)),
    );
    export.insert(
        "efs_access_point_arn".into(),
        json!(format!("{}-efs-access-point", vpc.name)),
    );
    export.insert(
        "efs_security_group".into(),
        json!(format!("{}-efs-security-group", vpc.name)),
    );
    export
}

/// `efs_filesystem_id`: `[vpc.efs]` があるときの EFS の filesystem id (stack の外で
/// 作る。未作成なら `None`)
pub fn vpc_stack(vpc: &VpcContext, efs_filesystem_id: Option<&str>) -> Result<StackTemplate> {
    let name = stack_name(vpc);
    let export = export(vpc);
    let mut extra = Map::new();
    let filesystem_id = if vpc.efs.is_some() {
        efs_filesystem_id
    } else {
        None
    };
    extra.insert("efs_filesystem_id".into(), json!(filesystem_id));
    let vars = variables(&name, &export, extra, dump(vpc));
    let mut tags = Vec::new();
    if vpc.sharable {
        tags.push(("pocket:sharable".to_string(), "true".to_string()));
    }
    Ok(StackTemplate {
        body: render_template("vpc", &vars)?,
        name,
        region: vpc.region.clone(),
        capabilities: Vec::new(),
        tags,
        export,
    })
}
