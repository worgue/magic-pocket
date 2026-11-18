//! AWS Backup の plan / selection の管理 (Python 版 `resources/backup.py`)。
//!
//! plan は対象の cluster が出来てからでないと selection を張れないので、作業は deploy の
//! 最後 (`ensure_post_deploy_state`) に行う。backup の「設定」(plan / selection / role)
//! は stage の付属物として作り・消すが、「データ」(vault と recovery point) は消さない。
//!
//! backup の権限を持たない deploy role でも deploy / destroy を通せるよう、
//! `AccessDeniedException` は Python 版と同じ箇所で警告に留める。

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{anyhow, Result};
use aws_config::SdkConfig;
use aws_sdk_backup::types::{BackupPlanInput, BackupRuleInput, BackupSelection, Lifecycle};
use magic_pocket_settings::context::{BackupContext, BackupPlanContext};
use serde_json::{json, Map, Value as Json};

use crate::aws_util::{error_code, has_code, regional_config, sdk_error};
use crate::stack::roles::{self, RoleSpec};
use crate::status::sink::Sink;

const ACCESS_DENIED: &str = "AccessDeniedException";
/// plan が宣言どおりかの判定で比べる項目。AWS が既定値で補う項目 (StartWindowMinutes や
/// RuleId など) を含めると毎 deploy で更新が走るので、pocket が宣言した項目だけを見る
const PLAN_RULE_KEYS: [&str; 3] = [
    "TargetBackupVaultName",
    "ScheduleExpression",
    "ScheduleExpressionTimezone",
];
const PLAN_LIFECYCLE_KEYS: [&str; 2] = ["MoveToColdStorageAfterDays", "DeleteAfterDays"];

pub trait BackupApi {
    /// 名前の一致する plan の id (全ページを探す)
    async fn find_plan_id(&self, plan_name: &str) -> Result<Option<String>>;
    /// vault を作る。既に在れば `false`
    async fn create_vault(&self, name: &str) -> Result<bool>;
    /// サービスロールを確保して ARN を返す
    async fn ensure_role(&self, spec: &RoleSpec) -> Result<String>;
    async fn delete_role(&self, spec: &RoleSpec) -> Result<()>;
    async fn create_plan(&self, document: &Json) -> Result<String>;
    /// 今の plan (boto3 が返す形のうち、比べる項目)
    async fn get_plan(&self, plan_id: &str) -> Result<Json>;
    async fn update_plan(&self, plan_id: &str, document: &Json) -> Result<()>;
    async fn delete_plan(&self, plan_id: &str) -> Result<()>;
    /// plan の selection の (id, 名前)
    async fn selections(&self, plan_id: &str) -> Result<Vec<(String, String)>>;
    /// selection の `Resources` と `IamRoleArn`
    async fn get_selection(&self, plan_id: &str, selection_id: &str) -> Result<Json>;
    async fn create_selection(&self, plan_id: &str, document: &Json) -> Result<()>;
    async fn delete_selection(&self, plan_id: &str, selection_id: &str) -> Result<()>;
}

/// plan の文書 (Python の `_plan_document`)
pub fn plan_document(backup: &BackupContext, plan: &BackupPlanContext) -> Json {
    let rules: Vec<Json> = plan
        .rules
        .iter()
        .map(|rule| {
            let mut lifecycle = Map::new();
            lifecycle.insert("DeleteAfterDays".to_string(), json!(rule.delete_after_days));
            if rule.cold_storage_after_days != 0 {
                lifecycle.insert(
                    "MoveToColdStorageAfterDays".to_string(),
                    json!(rule.cold_storage_after_days),
                );
            }
            json!({
                "RuleName": rule.name,
                "TargetBackupVaultName": backup.vault_name,
                "ScheduleExpression": rule.schedule_expression,
                "ScheduleExpressionTimezone": backup.timezone,
                "Lifecycle": lifecycle,
            })
        })
        .collect();
    json!({"BackupPlanName": plan.plan_name, "Rules": rules})
}

/// selection の文書 (名前は plan と同じ)
pub fn selection_document(plan: &BackupPlanContext, role_arn: &str, arns: &[String]) -> Json {
    json!({
        "SelectionName": plan.plan_name,
        "IamRoleArn": role_arn,
        "Resources": arns,
    })
}

/// 値が無い (key が無い・null) なら `None`
fn field<'j>(value: Option<&'j Json>, key: &str) -> Option<&'j Json> {
    value.and_then(|v| v.get(key)).filter(|v| !v.is_null())
}

fn rules_by_name(plan: &Json) -> BTreeMap<&str, &Json> {
    let rules = plan["Rules"].as_array();
    rules
        .into_iter()
        .flatten()
        .map(|rule| (rule["RuleName"].as_str().unwrap_or_default(), rule))
        .collect()
}

/// 今の plan が宣言どおりか。rule を名前で突き合わせ、宣言した項目だけを比べる
pub fn plan_matches(current: &Json, desired: &Json) -> bool {
    let current_rules = rules_by_name(current);
    let desired_rules = rules_by_name(desired);
    let current_names: BTreeSet<&str> = current_rules.keys().copied().collect();
    let desired_names: BTreeSet<&str> = desired_rules.keys().copied().collect();
    if current_names != desired_names {
        return false;
    }
    desired_rules.iter().all(|(name, want)| {
        let have = current_rules[name];
        let same = |keys: &[&str], have: Option<&Json>, want: Option<&Json>| {
            keys.iter().all(|key| field(have, key) == field(want, key))
        };
        same(&PLAN_RULE_KEYS, Some(have), Some(want))
            && same(
                &PLAN_LIFECYCLE_KEYS,
                field(Some(have), "Lifecycle"),
                field(Some(want), "Lifecycle"),
            )
    })
}

fn denied(error: &anyhow::Error, codes: &[&str]) -> bool {
    error_code(error).is_some_and(|code| codes.contains(&code))
}

pub struct BackupDeploy<'a, A> {
    pub api: &'a A,
}

impl<A: BackupApi> BackupDeploy<'_, A> {
    /// 宣言された plan と selection を確保する。
    ///
    /// `targets` は service (`dsql` / `rds`) → 対象の ARN (cluster が未作成の service は
    /// 入れない)。宣言が無ければ plan は作らない。ただし旧形式名の plan は宣言によらず
    /// 消す (改名で孤児になっており、残ると新しい plan と二重に走る)
    pub async fn ensure_post_deploy_state(
        &self,
        backup: &BackupContext,
        targets: &BTreeMap<String, String>,
        sink: &mut dyn Sink,
    ) -> Result<()> {
        self.cleanup_legacy_plans(backup, sink).await?;
        if backup.plans.is_empty() {
            return Ok(());
        }
        match self.ensure_plans(backup, targets, sink).await {
            Err(error) if denied(&error, &[ACCESS_DENIED]) => {
                sink.warning(&format!(
                    "AWS Backup の設定に必要な権限が無いため、定期バックアップの \
                     provisioning をスキップしました: {error}"
                ));
                sink.warning(
                    "  deploy role に backup グループの権限 (backup:* と kms 系) \
                     を付与してください (`pocket permissions list` 参照)。",
                );
                Ok(())
            }
            result => result,
        }
    }

    async fn ensure_plans(
        &self,
        backup: &BackupContext,
        targets: &BTreeMap<String, String>,
        sink: &mut dyn Sink,
    ) -> Result<()> {
        let planned: Vec<(&BackupPlanContext, &String)> = backup
            .plans
            .iter()
            .filter_map(|plan| Some((plan, targets.get(&plan.service)?)))
            .collect();
        if planned.is_empty() {
            return Ok(());
        }
        // vault が 1 つも無い account では、無い vault の describe が「見つからない」
        // ではなく権限不足を返す。作成を先に撃って「既にある」を受ける
        if self.api.create_vault(&backup.vault_name).await? {
            sink.log(&format!("Created backup vault: {}", backup.vault_name));
        }
        let role = roles::backup_role(
            &backup.role_name,
            backup.permissions_boundary.as_deref(),
            backup.external_roles,
        );
        let role_arn = self.api.ensure_role(&role).await?;
        for (plan, arn) in planned {
            let plan_id = self.ensure_plan(backup, plan, sink).await?;
            self.ensure_selection(plan, &plan_id, &role_arn, std::slice::from_ref(arn), sink)
                .await?;
        }
        Ok(())
    }

    /// 0.27 以前の旧形式名の plan を消す。権限が無ければ警告に留める
    async fn cleanup_legacy_plans(
        &self,
        backup: &BackupContext,
        sink: &mut dyn Sink,
    ) -> Result<()> {
        for plan_name in &backup.legacy_plan_names {
            let result = async {
                let Some(plan_id) = self.api.find_plan_id(plan_name).await? else {
                    return Ok(());
                };
                sink.log(&format!("Deleting legacy backup plan: {plan_name}"));
                self.delete_plan(&plan_id).await
            }
            .await;
            match result {
                Err(error) if denied(&error, &[ACCESS_DENIED]) => sink.warning(&format!(
                    "旧形式の backup plan ({plan_name}) を確認する権限が無いため、\
                     掃除をスキップしました。plan が残っている場合は二重に\
                     バックアップが走るため、手動で削除してください。"
                )),
                result => result?,
            }
        }
        Ok(())
    }

    async fn ensure_plan(
        &self,
        backup: &BackupContext,
        plan: &BackupPlanContext,
        sink: &mut dyn Sink,
    ) -> Result<String> {
        let document = plan_document(backup, plan);
        let Some(plan_id) = self.api.find_plan_id(&plan.plan_name).await? else {
            sink.log(&format!("Creating backup plan: {}", plan.plan_name));
            return self.api.create_plan(&document).await;
        };
        if !plan_matches(&self.api.get_plan(&plan_id).await?, &document) {
            sink.log(&format!("Updating backup plan: {}", plan.plan_name));
            self.api.update_plan(&plan_id, &document).await?;
        }
        Ok(plan_id)
    }

    /// 対象の ARN を指す selection を確保する。selection は更新できないので、対象か
    /// role が変わっていたら消して作り直す
    async fn ensure_selection(
        &self,
        plan: &BackupPlanContext,
        plan_id: &str,
        role_arn: &str,
        arns: &[String],
        sink: &mut dyn Sink,
    ) -> Result<()> {
        for (selection_id, name) in self.api.selections(plan_id).await? {
            if name != plan.plan_name {
                continue;
            }
            let current = self.api.get_selection(plan_id, &selection_id).await?;
            let mut resources: Vec<&str> = current["Resources"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Json::as_str)
                .collect();
            resources.sort_unstable();
            if resources == arns && current["IamRoleArn"] == role_arn {
                return Ok(());
            }
            self.api.delete_selection(plan_id, &selection_id).await?;
        }
        sink.log(&format!("Creating backup selection for {}", plan.plan_name));
        let document = selection_document(plan, role_arn, arns);
        self.api.create_selection(plan_id, &document).await
    }

    /// selection → plan の順に消す (selection が残ると plan を消せない)
    async fn delete_plan(&self, plan_id: &str) -> Result<()> {
        for (selection_id, _) in self.api.selections(plan_id).await? {
            self.api.delete_selection(plan_id, &selection_id).await?;
        }
        self.api.delete_plan(plan_id).await
    }

    /// plan / selection とサービスロールを消す (vault と recovery point は残す)。
    ///
    /// 宣言の有無によらず、stage に在りうる全ての plan 名を探す
    pub async fn delete(&self, backup: &BackupContext, sink: &mut dyn Sink) -> Result<()> {
        self.delete_plans(backup, sink).await?;
        // external の role は利用者の持ち物なので消さない
        if backup.external_roles {
            return Ok(());
        }
        let role = roles::backup_role(&backup.role_name, None, false);
        match self.api.delete_role(&role).await {
            Err(error) if denied(&error, &["AccessDenied", ACCESS_DENIED]) => {
                sink.warning(&format!(
                    "backup サービスロール ({}) を削除する権限がないため、スキップ\
                     しました。不要なら手動で削除してください。",
                    backup.role_name
                ));
                Ok(())
            }
            result => result,
        }
    }

    async fn delete_plans(&self, backup: &BackupContext, sink: &mut dyn Sink) -> Result<()> {
        for plan_name in &backup.cleanup_plan_names {
            let plan_id = match self.api.find_plan_id(plan_name).await {
                Err(error) if denied(&error, &[ACCESS_DENIED]) => {
                    sink.warning(
                        "backup plan を確認する権限がないため、削除をスキップしました \
                         (backup:ListBackupPlans)。plan が残っている場合は手動で \
                         削除してください。",
                    );
                    return Ok(());
                }
                found => found?,
            };
            let Some(plan_id) = plan_id else {
                continue;
            };
            self.delete_plan(&plan_id).await?;
            sink.log(&format!("Deleted backup plan: {plan_name}"));
        }
        Ok(())
    }
}

/// AWS に対する [`BackupApi`]
pub struct AwsBackup<'a> {
    pub sdk: &'a SdkConfig,
    pub region: &'a str,
}

impl AwsBackup<'_> {
    fn client(&self) -> aws_sdk_backup::Client {
        aws_sdk_backup::Client::new(&regional_config(self.sdk, self.region))
    }

    fn iam(&self) -> aws_sdk_iam::Client {
        aws_sdk_iam::Client::new(&regional_config(self.sdk, self.region))
    }
}

fn string(value: &Json) -> Option<String> {
    value.as_str().map(str::to_string)
}

/// plan の文書 (boto3 の形) を SDK の型にする
fn plan_input(document: &Json) -> Result<BackupPlanInput> {
    let mut builder =
        BackupPlanInput::builder().set_backup_plan_name(string(&document["BackupPlanName"]));
    for rule in document["Rules"].as_array().into_iter().flatten() {
        let lifecycle = Lifecycle::builder()
            .set_delete_after_days(rule["Lifecycle"]["DeleteAfterDays"].as_i64())
            .set_move_to_cold_storage_after_days(
                rule["Lifecycle"]["MoveToColdStorageAfterDays"].as_i64(),
            )
            .build();
        let built = BackupRuleInput::builder()
            .set_rule_name(string(&rule["RuleName"]))
            .set_target_backup_vault_name(string(&rule["TargetBackupVaultName"]))
            .set_schedule_expression(string(&rule["ScheduleExpression"]))
            .set_schedule_expression_timezone(string(&rule["ScheduleExpressionTimezone"]))
            .lifecycle(lifecycle)
            .build()
            .map_err(|e| anyhow!("backup rule を組めません: {e}"))?;
        builder = builder.rules(built);
    }
    builder
        .build()
        .map_err(|e| anyhow!("backup plan を組めません: {e}"))
}

impl BackupApi for AwsBackup<'_> {
    async fn find_plan_id(&self, plan_name: &str) -> Result<Option<String>> {
        let mut pages = self.client().list_backup_plans().into_paginator().send();
        while let Some(page) = pages.next().await {
            let page = page.map_err(|e| sdk_error("backup list_backup_plans", &e))?;
            for plan in page.backup_plans_list() {
                if plan.backup_plan_name() == Some(plan_name) {
                    return Ok(plan.backup_plan_id().map(str::to_string));
                }
            }
        }
        Ok(None)
    }

    async fn create_vault(&self, name: &str) -> Result<bool> {
        let created = self
            .client()
            .create_backup_vault()
            .backup_vault_name(name)
            .send()
            .await;
        match created {
            Ok(_) => Ok(true),
            Err(e) if has_code(&e, &["AlreadyExistsException"]) => Ok(false),
            Err(e) => Err(sdk_error("backup create_backup_vault", &e)),
        }
    }

    async fn ensure_role(&self, spec: &RoleSpec) -> Result<String> {
        super::iam::ensure_role(&self.iam(), spec).await
    }

    async fn delete_role(&self, spec: &RoleSpec) -> Result<()> {
        super::iam::delete_role(&self.iam(), &spec.name, &spec.managed_policy_arns).await?;
        Ok(())
    }

    async fn create_plan(&self, document: &Json) -> Result<String> {
        let created = self
            .client()
            .create_backup_plan()
            .backup_plan(plan_input(document)?)
            .send()
            .await
            .map_err(|e| sdk_error("backup create_backup_plan", &e))?;
        created
            .backup_plan_id()
            .map(str::to_string)
            .ok_or_else(|| anyhow!("backup create_backup_plan が plan の id を返しませんでした"))
    }

    async fn get_plan(&self, plan_id: &str) -> Result<Json> {
        let response = self
            .client()
            .get_backup_plan()
            .backup_plan_id(plan_id)
            .send()
            .await
            .map_err(|e| sdk_error("backup get_backup_plan", &e))?;
        let Some(plan) = response.backup_plan() else {
            return Ok(json!({}));
        };
        let rules: Vec<Json> = plan
            .rules()
            .iter()
            .map(|rule| {
                let mut item = Map::new();
                item.insert("RuleName".to_string(), json!(rule.rule_name()));
                item.insert(
                    "TargetBackupVaultName".to_string(),
                    json!(rule.target_backup_vault_name()),
                );
                if let Some(expression) = rule.schedule_expression() {
                    item.insert("ScheduleExpression".to_string(), json!(expression));
                }
                if let Some(timezone) = rule.schedule_expression_timezone() {
                    item.insert("ScheduleExpressionTimezone".to_string(), json!(timezone));
                }
                if let Some(lifecycle) = rule.lifecycle() {
                    let mut value = Map::new();
                    if let Some(days) = lifecycle.move_to_cold_storage_after_days() {
                        value.insert("MoveToColdStorageAfterDays".to_string(), json!(days));
                    }
                    if let Some(days) = lifecycle.delete_after_days() {
                        value.insert("DeleteAfterDays".to_string(), json!(days));
                    }
                    item.insert("Lifecycle".to_string(), Json::Object(value));
                }
                Json::Object(item)
            })
            .collect();
        Ok(json!({"BackupPlanName": plan.backup_plan_name(), "Rules": rules}))
    }

    async fn update_plan(&self, plan_id: &str, document: &Json) -> Result<()> {
        self.client()
            .update_backup_plan()
            .backup_plan_id(plan_id)
            .backup_plan(plan_input(document)?)
            .send()
            .await
            .map_err(|e| sdk_error("backup update_backup_plan", &e))?;
        Ok(())
    }

    async fn delete_plan(&self, plan_id: &str) -> Result<()> {
        self.client()
            .delete_backup_plan()
            .backup_plan_id(plan_id)
            .send()
            .await
            .map_err(|e| sdk_error("backup delete_backup_plan", &e))?;
        Ok(())
    }

    async fn selections(&self, plan_id: &str) -> Result<Vec<(String, String)>> {
        let mut pages = self
            .client()
            .list_backup_selections()
            .backup_plan_id(plan_id)
            .into_paginator()
            .send();
        let mut selections = Vec::new();
        while let Some(page) = pages.next().await {
            let page = page.map_err(|e| sdk_error("backup list_backup_selections", &e))?;
            for item in page.backup_selections_list() {
                selections.push((
                    item.selection_id().unwrap_or_default().to_string(),
                    item.selection_name().unwrap_or_default().to_string(),
                ));
            }
        }
        Ok(selections)
    }

    async fn get_selection(&self, plan_id: &str, selection_id: &str) -> Result<Json> {
        let response = self
            .client()
            .get_backup_selection()
            .backup_plan_id(plan_id)
            .selection_id(selection_id)
            .send()
            .await
            .map_err(|e| sdk_error("backup get_backup_selection", &e))?;
        Ok(match response.backup_selection() {
            Some(selection) => json!({
                "Resources": selection.resources(),
                "IamRoleArn": selection.iam_role_arn(),
            }),
            None => json!({}),
        })
    }

    async fn create_selection(&self, plan_id: &str, document: &Json) -> Result<()> {
        let resources: Vec<String> = document["Resources"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(string)
            .collect();
        let selection = BackupSelection::builder()
            .set_selection_name(string(&document["SelectionName"]))
            .set_iam_role_arn(string(&document["IamRoleArn"]))
            .set_resources(Some(resources))
            .build()
            .map_err(|e| anyhow!("backup selection を組めません: {e}"))?;
        self.client()
            .create_backup_selection()
            .backup_plan_id(plan_id)
            .backup_selection(selection)
            .send()
            .await
            .map_err(|e| sdk_error("backup create_backup_selection", &e))?;
        Ok(())
    }

    async fn delete_selection(&self, plan_id: &str, selection_id: &str) -> Result<()> {
        self.client()
            .delete_backup_selection()
            .backup_plan_id(plan_id)
            .selection_id(selection_id)
            .send()
            .await
            .map_err(|e| sdk_error("backup delete_backup_selection", &e))?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;

    use super::*;
    use crate::aws_util::AwsError;
    use crate::deploy::poll::testing::run;
    use crate::stack::testing::{context, read_json, repo_root, vector_context, vector_dirs};
    use crate::status::sink::{Recorded, Style};

    const ROLE_ARN: &str = "arn:aws:iam::123456789012:role/backup";
    const DSQL_ARN: &str = "arn:aws:dsql:ap-northeast-1:123456789012:cluster/abc";

    #[derive(Default, Clone)]
    struct Plan {
        id: String,
        document: Json,
        /// (id, 文書)
        selections: Vec<(String, Json)>,
    }

    /// plan と selection を持つ fake。`deny` に入れた操作は権限不足で失敗する
    #[derive(Default)]
    struct FakeBackup {
        plans: RefCell<Vec<Plan>>,
        vault: RefCell<bool>,
        deny: Vec<(&'static str, &'static str)>,
        calls: RefCell<Vec<String>>,
        next_id: RefCell<u32>,
    }

    impl FakeBackup {
        fn call(&self, operation: &str, detail: &str) -> Result<()> {
            if let Some((_, code)) = self.deny.iter().find(|(name, _)| *name == operation) {
                return Err(anyhow::Error::new(AwsError {
                    operation: operation.to_string(),
                    code: code.to_string(),
                    message: "denied".to_string(),
                }));
            }
            self.calls
                .borrow_mut()
                .push(format!("{operation} {detail}"));
            Ok(())
        }

        fn writes(&self) -> Vec<String> {
            let reads = ["find_plan_id", "get_plan", "selections", "get_selection"];
            let calls = self.calls.borrow();
            calls
                .iter()
                .filter(|call| !reads.iter().any(|read| call.starts_with(read)))
                .cloned()
                .collect()
        }

        fn new_id(&self, prefix: &str) -> String {
            *self.next_id.borrow_mut() += 1;
            format!("{prefix}-{}", self.next_id.borrow())
        }

        fn name(document: &Json) -> &str {
            document["BackupPlanName"].as_str().unwrap_or_default()
        }

        fn add_plan(&self, document: Json, selections: Vec<Json>) -> String {
            let id = self.new_id("plan");
            let selections = selections
                .into_iter()
                .map(|selection| (self.new_id("sel"), selection))
                .collect();
            self.plans.borrow_mut().push(Plan {
                id: id.clone(),
                document,
                selections,
            });
            id
        }

        fn plan(&self, name: &str) -> Option<Plan> {
            let plans = self.plans.borrow();
            plans
                .iter()
                .find(|p| Self::name(&p.document) == name)
                .cloned()
        }
    }

    impl BackupApi for FakeBackup {
        async fn find_plan_id(&self, plan_name: &str) -> Result<Option<String>> {
            self.call("find_plan_id", plan_name)?;
            Ok(self.plan(plan_name).map(|plan| plan.id))
        }

        async fn create_vault(&self, name: &str) -> Result<bool> {
            self.call("create_vault", name)?;
            Ok(!self.vault.replace(true))
        }

        async fn ensure_role(&self, spec: &RoleSpec) -> Result<String> {
            self.call("ensure_role", &spec.name)?;
            Ok(ROLE_ARN.to_string())
        }

        async fn delete_role(&self, spec: &RoleSpec) -> Result<()> {
            self.call("delete_role", &spec.name)
        }

        async fn create_plan(&self, document: &Json) -> Result<String> {
            self.call("create_plan", Self::name(document))?;
            Ok(self.add_plan(document.clone(), Vec::new()))
        }

        async fn get_plan(&self, plan_id: &str) -> Result<Json> {
            self.call("get_plan", plan_id)?;
            let plans = self.plans.borrow();
            Ok(plans
                .iter()
                .find(|p| p.id == plan_id)
                .map_or(json!({}), |p| p.document.clone()))
        }

        async fn update_plan(&self, plan_id: &str, document: &Json) -> Result<()> {
            self.call("update_plan", Self::name(document))?;
            let mut plans = self.plans.borrow_mut();
            if let Some(plan) = plans.iter_mut().find(|p| p.id == plan_id) {
                plan.document = document.clone();
            }
            Ok(())
        }

        async fn delete_plan(&self, plan_id: &str) -> Result<()> {
            self.call("delete_plan", plan_id)?;
            self.plans.borrow_mut().retain(|p| p.id != plan_id);
            Ok(())
        }

        async fn selections(&self, plan_id: &str) -> Result<Vec<(String, String)>> {
            self.call("selections", plan_id)?;
            let plans = self.plans.borrow();
            let plan = plans.iter().find(|p| p.id == plan_id);
            Ok(plan
                .map(|plan| {
                    plan.selections
                        .iter()
                        .map(|(id, doc)| {
                            let name = doc["SelectionName"].as_str().unwrap_or_default();
                            (id.clone(), name.to_string())
                        })
                        .collect()
                })
                .unwrap_or_default())
        }

        async fn get_selection(&self, plan_id: &str, selection_id: &str) -> Result<Json> {
            self.call("get_selection", selection_id)?;
            let plans = self.plans.borrow();
            let plan = plans.iter().find(|p| p.id == plan_id);
            let found = plan.and_then(|p| p.selections.iter().find(|(id, _)| id == selection_id));
            Ok(found.map_or(json!({}), |(_, doc)| doc.clone()))
        }

        async fn create_selection(&self, plan_id: &str, document: &Json) -> Result<()> {
            let name = document["SelectionName"].as_str().unwrap_or_default();
            self.call("create_selection", name)?;
            let id = self.new_id("sel");
            let mut plans = self.plans.borrow_mut();
            if let Some(plan) = plans.iter_mut().find(|p| p.id == plan_id) {
                plan.selections.push((id, document.clone()));
            }
            Ok(())
        }

        async fn delete_selection(&self, plan_id: &str, selection_id: &str) -> Result<()> {
            self.call("delete_selection", selection_id)?;
            let mut plans = self.plans.borrow_mut();
            if let Some(plan) = plans.iter_mut().find(|p| p.id == plan_id) {
                plan.selections.retain(|(id, _)| id != selection_id);
            }
            Ok(())
        }
    }

    /// dsql と rds の plan を宣言した backup
    fn backup() -> BackupContext {
        let backup = vector_context("toml_vectors/db_backup.prod")
            .backup
            .expect("backup");
        assert!(backup.plans.len() >= 2 && !backup.legacy_plan_names.is_empty());
        backup
    }

    fn targets(services: &[&str]) -> BTreeMap<String, String> {
        services
            .iter()
            .map(|service| (service.to_string(), format!("arn:{service}")))
            .collect()
    }

    fn ensure(api: &FakeBackup, backup: &BackupContext, services: &[&str]) -> Recorded {
        let mut sink = Recorded::default();
        let deploy = BackupDeploy { api };
        run(deploy.ensure_post_deploy_state(backup, &targets(services), &mut sink))
            .expect("ensure");
        sink
    }

    fn warnings(sink: &Recorded) -> Vec<&str> {
        sink.0
            .iter()
            .filter(|record| record.1 == Style::Warning)
            .map(|record| record.2.as_str())
            .collect()
    }

    #[test]
    fn test_documents_match_python() {
        let root = repo_root();
        let (mut plans, mut cases) = (0, 0);
        for dir in vector_dirs() {
            let path = dir.join("documents.json");
            if !path.is_file() {
                continue;
            }
            let documents = read_json(&path);
            let want = &documents["backup"];
            if want.is_null() {
                continue;
            }
            let ctx = context(&root, &read_json(&dir.join("manifest.json")));
            let backup = ctx.backup.as_ref().expect("backup");
            let wanted = want["plans"].as_array().expect("plans");
            assert_eq!(backup.plans.len(), wanted.len(), "{}", dir.display());
            for (plan, want_plan) in backup.plans.iter().zip(wanted) {
                let label = format!("{} {}", dir.display(), plan.plan_name);
                let document = plan_document(backup, plan);
                assert_eq!(document, want_plan["document"], "{label}");
                let arns = [want["target_arn"].as_str().expect("arn").to_string()];
                let role = want["role_arn"].as_str().expect("role");
                assert_eq!(
                    selection_document(plan, role, &arns),
                    want_plan["selection"],
                    "{label}"
                );
                for case in want_plan["match_cases"].as_array().expect("cases") {
                    assert_eq!(
                        json!(plan_matches(&case["current"], &document)),
                        case["matches"],
                        "{label} {}",
                        case["name"]
                    );
                    cases += 1;
                }
                plans += 1;
            }
        }
        assert!(
            plans >= 3 && cases >= 40,
            "vector が少なすぎます: {plans} / {cases}"
        );
    }

    #[test]
    fn test_ensure_creates_vault_role_plan_and_selection() {
        let backup = backup();
        let api = FakeBackup::default();
        let sink = ensure(&api, &backup, &["dsql", "rds"]);
        let mut expected = vec![
            format!("create_vault {}", backup.vault_name),
            format!("ensure_role {}", backup.role_name),
        ];
        for plan in &backup.plans {
            expected.push(format!("create_plan {}", plan.plan_name));
            expected.push(format!("create_selection {}", plan.plan_name));
        }
        assert_eq!(api.writes(), expected);
        let plan = api.plan(&backup.plans[0].plan_name).expect("plan");
        let service = &backup.plans[0].service;
        assert_eq!(
            plan.selections[0].1,
            selection_document(&backup.plans[0], ROLE_ARN, &[format!("arn:{service}")])
        );
        assert!(warnings(&sink).is_empty());

        // 2 回目は何も書かない (vault は「既にある」を受けるだけ)
        api.calls.borrow_mut().clear();
        ensure(&api, &backup, &["dsql", "rds"]);
        assert_eq!(
            api.writes(),
            [
                format!("create_vault {}", backup.vault_name),
                format!("ensure_role {}", backup.role_name),
            ]
        );
    }

    #[test]
    fn test_ensure_skips_without_declaration_or_targets() {
        // 宣言が無い: 旧形式の plan の確認だけ
        let mut undeclared = backup();
        undeclared.plans.clear();
        let api = FakeBackup::default();
        ensure(&api, &undeclared, &["dsql", "rds"]);
        assert!(api.writes().is_empty());

        // 対象の cluster が無い: vault も作らない
        let backup = backup();
        let api = FakeBackup::default();
        ensure(&api, &backup, &[]);
        assert!(api.writes().is_empty());

        // 片方だけ在る: その service の plan だけを作る
        let api = FakeBackup::default();
        ensure(&api, &backup, &["dsql"]);
        let created: Vec<String> = api
            .writes()
            .into_iter()
            .filter(|call| call.starts_with("create_plan"))
            .collect();
        let dsql_plan = backup.plans.iter().find(|p| p.service == "dsql").unwrap();
        assert_eq!(created, [format!("create_plan {}", dsql_plan.plan_name)]);
    }

    #[test]
    fn test_ensure_updates_drifted_plan_and_recreates_selection() {
        let backup = backup();
        let plan = backup.plans.iter().find(|p| p.service == "dsql").unwrap();
        let mut drifted = plan_document(&backup, plan);
        drifted["Rules"][0]["ScheduleExpression"] = json!("cron(0 0 1 1 ? 2000)");
        // 対象の ARN が古い selection と、名前の違う selection
        let stale = selection_document(plan, ROLE_ARN, &["arn:old".to_string()]);
        let other = json!({"SelectionName": "manual", "Resources": ["arn:x"]});
        let api = FakeBackup::default();
        api.add_plan(drifted, vec![stale, other.clone()]);
        ensure(&api, &backup, &["dsql"]);
        let writes = api.writes();
        assert!(writes.contains(&format!("update_plan {}", plan.plan_name)));
        let position = |prefix: &str| writes.iter().position(|w| w.starts_with(prefix));
        assert!(position("delete_selection") < position("create_selection"));
        let stored = api.plan(&plan.plan_name).expect("plan");
        assert_eq!(stored.document, plan_document(&backup, plan));
        let selections: Vec<&Json> = stored.selections.iter().map(|(_, doc)| doc).collect();
        // 名前の違う selection には触らない
        assert_eq!(
            selections,
            [
                &other,
                &selection_document(plan, ROLE_ARN, &["arn:dsql".to_string()])
            ]
        );
    }

    #[test]
    fn test_selection_is_recreated_when_role_changes() {
        let backup = backup();
        let plan = backup.plans.iter().find(|p| p.service == "dsql").unwrap();
        let old_role = selection_document(plan, "arn:aws:iam::1:role/old", &["arn:dsql".into()]);
        let api = FakeBackup::default();
        api.add_plan(plan_document(&backup, plan), vec![old_role]);
        ensure(&api, &backup, &["dsql"]);
        let writes = api.writes();
        assert!(writes.iter().any(|w| w.starts_with("delete_selection")));
        assert!(writes.iter().any(|w| w.starts_with("create_selection")));
        assert!(!writes.iter().any(|w| w.starts_with("update_plan")));
    }

    #[test]
    fn test_legacy_plans_are_deleted_regardless_of_declaration() {
        let mut backup = backup();
        let legacy = backup.legacy_plan_names[0].clone();
        backup.plans.clear();
        let api = FakeBackup::default();
        let selection = json!({"SelectionName": legacy, "Resources": [DSQL_ARN]});
        api.add_plan(
            json!({"BackupPlanName": legacy, "Rules": []}),
            vec![selection],
        );
        let sink = ensure(&api, &backup, &[]);
        let writes = api.writes();
        assert_eq!(writes.len(), 2);
        assert!(writes[0].starts_with("delete_selection") && writes[1].starts_with("delete_plan"));
        assert!(sink
            .0
            .iter()
            .any(|r| r.2 == format!("Deleting legacy backup plan: {legacy}")));
    }

    #[test]
    fn test_access_denied_is_a_warning_at_each_step() {
        let backup = backup();
        // 旧形式の plan の確認が拒否されても、宣言した plan の確保へ進む。そこでも
        // 一覧が拒否されるので、vault と role を確保した所で打ち切って警告する
        let api = FakeBackup {
            deny: vec![("find_plan_id", ACCESS_DENIED)],
            ..Default::default()
        };
        let sink = ensure(&api, &backup, &["dsql", "rds"]);
        let warned = warnings(&sink);
        assert_eq!(warned.len(), backup.legacy_plan_names.len() + 2);
        assert!(warned[0].starts_with("旧形式の backup plan ("));
        assert!(warned[warned.len() - 2].starts_with("AWS Backup の設定に必要な権限が無いため"));
        assert_eq!(api.writes().len(), 2, "vault と role まで");

        // 途中の操作ごとに: 拒否された所で打ち切り、後続の plan へは進まない
        for operation in [
            "create_vault",
            "ensure_role",
            "create_plan",
            "create_selection",
        ] {
            let api = FakeBackup {
                deny: vec![(operation, ACCESS_DENIED)],
                ..Default::default()
            };
            let sink = ensure(&api, &backup, &["dsql", "rds"]);
            assert_eq!(warnings(&sink).len(), 2, "{operation}");
            let created = api
                .writes()
                .iter()
                .filter(|w| w.starts_with("create_plan"))
                .count();
            let expected = if operation == "create_selection" {
                1
            } else {
                0
            };
            assert_eq!(created, expected, "{operation}");
        }

        // selection を消した後の作り直しが拒否された場合も警告で終わる (selection は無い)
        let plan = backup.plans.iter().find(|p| p.service == "dsql").unwrap();
        let stale = selection_document(plan, ROLE_ARN, &["arn:old".to_string()]);
        let api = FakeBackup {
            deny: vec![("create_selection", ACCESS_DENIED)],
            ..Default::default()
        };
        api.add_plan(plan_document(&backup, plan), vec![stale]);
        let sink = ensure(&api, &backup, &["dsql"]);
        assert_eq!(warnings(&sink).len(), 2);
        assert!(api.plan(&plan.plan_name).unwrap().selections.is_empty());
    }

    #[test]
    fn test_other_errors_stop_the_deploy() {
        let backup = backup();
        // IAM の権限不足 (code は AccessDenied) は backup の権限不足として扱わない
        let api = FakeBackup {
            deny: vec![("ensure_role", "AccessDenied")],
            ..Default::default()
        };
        let mut sink = Recorded::default();
        let deploy = BackupDeploy { api: &api };
        let error = run(deploy.ensure_post_deploy_state(&backup, &targets(&["dsql"]), &mut sink))
            .expect_err("中止");
        assert_eq!(error_code(&error), Some("AccessDenied"));
    }

    #[test]
    fn test_delete_removes_plans_and_role_but_not_vault() {
        let backup = backup();
        let api = FakeBackup::default();
        ensure(&api, &backup, &["dsql", "rds"]);
        api.calls.borrow_mut().clear();
        let mut sink = Recorded::default();
        run(BackupDeploy { api: &api }.delete(&backup, &mut sink)).expect("delete");
        let writes = api.writes();
        assert!(api.plans.borrow().is_empty());
        assert_eq!(
            writes.last(),
            Some(&format!("delete_role {}", backup.role_name))
        );
        assert!(!writes.iter().any(|w| w.contains("vault")));
        assert!(*api.vault.borrow(), "vault は残す");
    }

    #[test]
    fn test_delete_warns_on_access_denied_and_keeps_external_role() {
        let backup = backup();
        // plan の一覧が拒否されたら plan の削除を打ち切り、role の削除へ進む
        let api = FakeBackup {
            deny: vec![
                ("find_plan_id", ACCESS_DENIED),
                ("delete_role", "AccessDenied"),
            ],
            ..Default::default()
        };
        let mut sink = Recorded::default();
        run(BackupDeploy { api: &api }.delete(&backup, &mut sink)).expect("delete");
        let warned = warnings(&sink);
        assert_eq!(warned.len(), 2);
        assert!(warned[0].starts_with("backup plan を確認する権限がないため"));
        assert!(warned[1].starts_with("backup サービスロール ("));

        // external の role は消さない
        let mut external = backup;
        external.external_roles = true;
        let api = FakeBackup::default();
        run(BackupDeploy { api: &api }.delete(&external, &mut sink)).expect("delete");
        assert!(!api.writes().iter().any(|w| w.starts_with("delete_role")));
    }
}
