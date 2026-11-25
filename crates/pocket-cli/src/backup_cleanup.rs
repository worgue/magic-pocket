//! `pocket backup cleanup` と `pocket cleanup-deprecated` (Python 版 `backup_cli.py` /
//! `cleanup_deprecated_cli.py` と `resources/backup.py` / `resources/aws/backup_legacy.py`)。
//!
//! どちらもバックアップの「データ」(recovery point) を消しうる操作なので、宣言と利用者の
//! 明示の確認を経た経路からだけ消す。
//!
//! - `backup cleanup`: この stage の pocket 管理 vault にある、今ある対象 DB (dsql /
//!   managed rds) の recovery point を消す。`[backup] deletable = true` の宣言が要る
//! - `cleanup-deprecated`: 0.36 以前の account 共有の vault (`pocket-backup`) とサービス
//!   ロール (`forge-pocket-backup-role`) を消す。まだ参照している plan / selection が
//!   あれば中止する

use std::time::Duration;

use anyhow::{bail, Result};
use clap::Subcommand;
use magic_pocket_rs::config::find_toml_path;
use magic_pocket_settings::Settings;

use crate::aws_util::{has_code, regional_config, sdk_error};
use crate::deploy::interaction::{Interaction, Prompt};
use crate::resource::dsql_backup::{
    is_access_denied, list_recovery_points_by_resource, py_iso, RecoveryPoint,
};
use crate::stack::roles;
use crate::status::sink::Sink;

pub const LEGACY_BACKUP_VAULT_NAME: &str = "pocket-backup";
pub const LEGACY_BACKUP_ROLE_NAME: &str = "forge-pocket-backup-role";

#[derive(Subcommand)]
pub enum BackupCommand {
    /// この stage のバックアップデータ (recovery point) を削除する。
    ///
    /// [backup] deletable = true の宣言が必要。この stage の pocket 管理 vault
    /// にある、現存する対象 DB (dsql / managed rds) の recovery point だけを消す。
    /// 削除済み cluster の分は ARN で引けないため対象外 (AWS Backup コンソールから
    /// 削除する)。plan (スケジュール) には触らない。
    Cleanup {
        /// stage 名
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        /// 確認プロンプトをスキップ
        #[arg(long, short = 'y')]
        yes: bool,
    },
}

// ---- backup cleanup ----

pub trait StagePoints {
    /// resource の recovery point
    async fn list(&self, resource_arn: &str) -> Result<Vec<RecoveryPoint>>;
    async fn delete(&self, vault: &str, arn: &str) -> Result<()>;
}

/// 対象 DB の recovery point のうち、pocket 管理の vault にあるもの。権限が無ければ `None`
async fn stage_points(
    api: &impl StagePoints,
    target_arns: &[String],
    vault_name: &str,
) -> Result<Option<Vec<RecoveryPoint>>> {
    let mut points = Vec::new();
    for arn in target_arns {
        match api.list(arn).await {
            Ok(found) => points.extend(found),
            Err(error) if is_access_denied(&error) => return Ok(None),
            Err(error) => return Err(error),
        }
    }
    // `--vault` で利用者の vault に取った分と、0.36 以前の共有 vault の分は消さない
    Ok(Some(
        points
            .into_iter()
            .filter(|point| point.vault_name.as_deref() == Some(vault_name))
            .collect(),
    ))
}

/// `backup cleanup` の本体。`target_arns` は今ある対象 DB の ARN (並べ替え済み)
pub async fn cleanup(
    api: &impl StagePoints,
    target_arns: &[String],
    vault_name: &str,
    prompt: &impl Prompt,
    sink: &mut dyn Sink,
) -> Result<()> {
    let Some(points) = stage_points(api, target_arns, vault_name).await? else {
        bail!("recovery point を列挙する権限がありません (backup:*)。");
    };
    if points.is_empty() {
        sink.info("削除対象の recovery point はありません。");
        return Ok(());
    }
    sink.warning("以下の recovery point を削除します:");
    for point in &points {
        let created = point.creation_date.map(py_iso);
        sink.info(&format!(
            "  - {} ({})",
            point.arn,
            created.as_deref().unwrap_or("-")
        ));
    }
    sink.danger("この操作は取り消せません！");
    let text = format!("{} 件の recovery point を削除しますか？", points.len());
    if !prompt.confirm(&text, false)? {
        bail!("Aborted!");
    }
    for point in &points {
        api.delete(
            point.vault_name.as_deref().unwrap_or(vault_name),
            &point.arn,
        )
        .await?;
    }
    sink.log(&format!("Deleted {} recovery point(s).", points.len()));
    sink.success(&format!(
        "{} 件の recovery point を削除しました。",
        points.len()
    ));
    Ok(())
}

struct AwsStagePoints {
    client: aws_sdk_backup::Client,
}

impl StagePoints for AwsStagePoints {
    async fn list(&self, resource_arn: &str) -> Result<Vec<RecoveryPoint>> {
        list_recovery_points_by_resource(&self.client, resource_arn).await
    }

    async fn delete(&self, vault: &str, arn: &str) -> Result<()> {
        delete_recovery_point(&self.client, vault, arn).await
    }
}

async fn delete_recovery_point(
    client: &aws_sdk_backup::Client,
    vault: &str,
    arn: &str,
) -> Result<()> {
    client
        .delete_recovery_point()
        .backup_vault_name(vault)
        .recovery_point_arn(arn)
        .send()
        .await
        .map_err(|e| sdk_error("backup delete_recovery_point", &e))?;
    Ok(())
}

pub async fn run_backup(command: BackupCommand, sink: &mut dyn Sink) -> Result<()> {
    let BackupCommand::Cleanup { stage, yes } = command;
    let (context, _) = crate::context::load_context(&stage)?;
    let Some(backup) = context.backup.as_ref() else {
        bail!("この stage には AWS Backup の対象 DB (dsql / managed rds) がありません。");
    };
    if backup.plans.is_empty() {
        bail!(
            "[backup] が宣言されていません。バックアップデータの削除には \
             [backup] の deletable = true 宣言が必要です。"
        );
    }
    if !backup.deletable {
        bail!(
            "バックアップデータ (recovery point) の削除には [backup] に \
             deletable = true の宣言が必要です。誤操作でデータを失わないための \
             ガードなので、削除する時だけ宣言することを推奨します。"
        );
    }
    let sdk = crate::aws_util::load_sdk().await;
    let mut targets: Vec<String> = crate::deploy::parts::backup_targets(&sdk, &context)
        .await?
        .into_values()
        .collect();
    targets.sort();
    let api = AwsStagePoints {
        client: aws_sdk_backup::Client::new(&regional_config(&sdk, &backup.region)),
    };
    let prompt = Interaction { assume_yes: yes };
    cleanup(&api, &targets, &backup.vault_name, &prompt, sink).await
}

// ---- cleanup-deprecated ----

/// 0.36 以前の共有リソースの操作
pub trait LegacyApi {
    /// backup plan の (id, 名前)
    async fn plans(&self) -> Result<Vec<(String, String)>>;
    /// plan の rule の保存先 vault
    async fn plan_vaults(&self, plan_id: &str) -> Result<Vec<String>>;
    /// plan の selection の (id, 名前)
    async fn selections(&self, plan_id: &str) -> Result<Vec<(String, String)>>;
    async fn selection_role(&self, plan_id: &str, selection_id: &str) -> Result<Option<String>>;
    async fn vault_exists(&self) -> Result<bool>;
    async fn vault_points(&self) -> Result<Vec<RecoveryPoint>>;
    async fn delete_point(&self, arn: &str) -> Result<()>;
    /// vault を消す。recovery point の削除が反映されておらず空でなければ `false`
    async fn delete_vault(&self) -> Result<bool>;
    async fn role_exists(&self) -> Result<bool>;
    /// role を消す。無ければ `false`
    async fn delete_role(&self) -> Result<bool>;
}

pub struct LegacyOptions {
    pub delete_recovery_points: bool,
    pub dry_run: bool,
    /// vault の削除を試す回数と間隔 (recovery point の削除は非同期で反映される)
    pub vault_attempts: u32,
    pub vault_interval: Duration,
}

/// 旧 vault / 旧 role をまだ参照している plan・selection の説明
async fn references(api: &impl LegacyApi) -> Result<Vec<String>> {
    let mut found = Vec::new();
    let suffix = format!(":role/{LEGACY_BACKUP_ROLE_NAME}");
    for (plan_id, plan_name) in api.plans().await? {
        if api
            .plan_vaults(&plan_id)
            .await?
            .iter()
            .any(|vault| vault == LEGACY_BACKUP_VAULT_NAME)
        {
            found.push(format!(
                "backup plan {plan_name} (保存先が {LEGACY_BACKUP_VAULT_NAME})"
            ));
        }
        for (selection_id, selection_name) in api.selections(&plan_id).await? {
            let role = api.selection_role(&plan_id, &selection_id).await?;
            if role.is_some_and(|arn| arn.ends_with(&suffix)) {
                found.push(format!(
                    "backup selection {plan_name} / {selection_name} (ロールが {LEGACY_BACKUP_ROLE_NAME})"
                ));
            }
        }
    }
    Ok(found)
}

/// 全 recovery point が失効し終える日時 (無期限のものがあれば `None`)
fn last_expiry(points: &[RecoveryPoint]) -> Option<String> {
    let mut latest = None;
    for point in points {
        let at = point.delete_at?;
        if latest.is_none_or(|current| at > current) {
            latest = Some(at);
        }
    }
    latest.map(|at| py_iso(at)[..10].to_string())
}

pub async fn cleanup_deprecated(
    api: &impl LegacyApi,
    region: &str,
    options: &LegacyOptions,
    prompt: &impl Prompt,
    sink: &mut dyn Sink,
) -> Result<()> {
    let found = references(api).await?;
    if !found.is_empty() {
        for reference in &found {
            sink.warning(&format!("  - {reference}"));
        }
        bail!(
            "旧リソースをまだ参照している設定があります (region: {region})。\
             該当する stage を 0.37.0 以上の pocket で deploy してから\
             再実行してください。"
        );
    }
    let vault_exists = api.vault_exists().await?;
    let role_exists = api.role_exists().await?;
    if !vault_exists && !role_exists {
        sink.success(&format!(
            "削除対象の旧リソースはありません (region: {region})。"
        ));
        return Ok(());
    }
    let points = if vault_exists {
        api.vault_points().await?
    } else {
        Vec::new()
    };
    let delete_vault = vault_exists && (points.is_empty() || options.delete_recovery_points);
    if vault_exists {
        sink.info(&format!(
            "backup vault: {LEGACY_BACKUP_VAULT_NAME} (recovery point {} 件)",
            points.len()
        ));
    }
    if role_exists {
        sink.info(&format!("IAM role: {LEGACY_BACKUP_ROLE_NAME}"));
    }
    if vault_exists && !delete_vault {
        let when = match last_expiry(&points) {
            Some(date) => format!("{date} 以降"),
            None => "無期限保持のものがあるため自動では空になりません".to_string(),
        };
        sink.warning(&format!(
            "recovery point が残っているため vault は削除しません。保持期限で失効して\
             空になった後 ({when}) に再実行するか、--delete-recovery-points で\
             データごと削除してください。"
        ));
    }
    if options.dry_run || !(delete_vault || role_exists) {
        return Ok(());
    }
    let text = if delete_vault && !points.is_empty() {
        format!(
            "旧 vault の recovery point {} 件を削除します。account 内の他 project の\
             バックアップデータも含まれ、復元できなくなります。続行しますか？",
            points.len()
        )
    } else {
        "上記の旧リソースを削除しますか？".to_string()
    };
    if !prompt.confirm(&text, false)? {
        bail!("Aborted!");
    }
    if role_exists && api.delete_role().await? {
        sink.log(&format!("Deleted IAM role: {LEGACY_BACKUP_ROLE_NAME}"));
    }
    if delete_vault && !delete_legacy_vault(api, &points, options, sink).await? {
        bail!(
            "recovery point の削除がまだ反映されておらず、vault を削除できません\
             でした。少し待って再実行してください。"
        );
    }
    sink.success("旧リソースの掃除が完了しました。");
    Ok(())
}

async fn delete_legacy_vault(
    api: &impl LegacyApi,
    points: &[RecoveryPoint],
    options: &LegacyOptions,
    sink: &mut dyn Sink,
) -> Result<bool> {
    if !points.is_empty() {
        for point in points {
            api.delete_point(&point.arn).await?;
        }
        sink.log(&format!("Deleted {} recovery points", points.len()));
    }
    // recovery point の削除は非同期で、直後の DeleteBackupVault は「空でない」と拒否される
    for attempt in 0..options.vault_attempts {
        if api.delete_vault().await? {
            sink.log(&format!("Deleted backup vault: {LEGACY_BACKUP_VAULT_NAME}"));
            return Ok(true);
        }
        if attempt + 1 < options.vault_attempts {
            tokio::time::sleep(options.vault_interval).await;
        }
    }
    Ok(false)
}

struct AwsLegacy {
    backup: aws_sdk_backup::Client,
    iam: aws_sdk_iam::Client,
}

impl LegacyApi for AwsLegacy {
    async fn plans(&self) -> Result<Vec<(String, String)>> {
        let mut pages = self.backup.list_backup_plans().into_paginator().send();
        let mut plans = Vec::new();
        while let Some(page) = pages.next().await {
            let page = page.map_err(|e| sdk_error("backup list_backup_plans", &e))?;
            for plan in page.backup_plans_list() {
                plans.push((
                    plan.backup_plan_id().unwrap_or_default().to_string(),
                    plan.backup_plan_name().unwrap_or_default().to_string(),
                ));
            }
        }
        Ok(plans)
    }

    async fn plan_vaults(&self, plan_id: &str) -> Result<Vec<String>> {
        let response = self
            .backup
            .get_backup_plan()
            .backup_plan_id(plan_id)
            .send()
            .await
            .map_err(|e| sdk_error("backup get_backup_plan", &e))?;
        Ok(response
            .backup_plan()
            .map(|plan| {
                plan.rules()
                    .iter()
                    .map(|rule| rule.target_backup_vault_name().to_string())
                    .collect()
            })
            .unwrap_or_default())
    }

    async fn selections(&self, plan_id: &str) -> Result<Vec<(String, String)>> {
        let mut pages = self
            .backup
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

    async fn selection_role(&self, plan_id: &str, selection_id: &str) -> Result<Option<String>> {
        let response = self
            .backup
            .get_backup_selection()
            .backup_plan_id(plan_id)
            .selection_id(selection_id)
            .send()
            .await
            .map_err(|e| sdk_error("backup get_backup_selection", &e))?;
        Ok(response
            .backup_selection()
            .map(|selection| selection.iam_role_arn().to_string()))
    }

    async fn vault_exists(&self) -> Result<bool> {
        // DescribeBackupVault は、vault が 1 つも無い account では「無い」でなく権限不足を
        // 返すので、一覧で調べる
        let mut pages = self.backup.list_backup_vaults().into_paginator().send();
        while let Some(page) = pages.next().await {
            let page = page.map_err(|e| sdk_error("backup list_backup_vaults", &e))?;
            if page
                .backup_vault_list()
                .iter()
                .any(|vault| vault.backup_vault_name() == Some(LEGACY_BACKUP_VAULT_NAME))
            {
                return Ok(true);
            }
        }
        Ok(false)
    }

    async fn vault_points(&self) -> Result<Vec<RecoveryPoint>> {
        let mut pages = self
            .backup
            .list_recovery_points_by_backup_vault()
            .backup_vault_name(LEGACY_BACKUP_VAULT_NAME)
            .into_paginator()
            .send();
        let mut points = Vec::new();
        while let Some(page) = pages.next().await {
            let page =
                page.map_err(|e| sdk_error("backup list_recovery_points_by_backup_vault", &e))?;
            points.extend(page.recovery_points().iter().map(|point| {
                RecoveryPoint {
                    arn: point.recovery_point_arn().unwrap_or_default().to_string(),
                    status: point.status().map(|s| s.as_str().to_string()),
                    creation_date: point.creation_date().copied(),
                    vault_name: point.backup_vault_name().map(str::to_string),
                    delete_at: point
                        .calculated_lifecycle()
                        .and_then(|lifecycle| lifecycle.delete_at())
                        .copied(),
                }
            }));
        }
        Ok(points)
    }

    async fn delete_point(&self, arn: &str) -> Result<()> {
        delete_recovery_point(&self.backup, LEGACY_BACKUP_VAULT_NAME, arn).await
    }

    async fn delete_vault(&self) -> Result<bool> {
        match self
            .backup
            .delete_backup_vault()
            .backup_vault_name(LEGACY_BACKUP_VAULT_NAME)
            .send()
            .await
        {
            Ok(_) => Ok(true),
            Err(e) if has_code(&e, &["InvalidRequestException"]) => Ok(false),
            Err(e) => Err(sdk_error("backup delete_backup_vault", &e)),
        }
    }

    async fn role_exists(&self) -> Result<bool> {
        match self
            .iam
            .get_role()
            .role_name(LEGACY_BACKUP_ROLE_NAME)
            .send()
            .await
        {
            Ok(_) => Ok(true),
            Err(e) if has_code(&e, &["NoSuchEntity"]) => Ok(false),
            Err(e) => Err(sdk_error(
                &format!("IAM get_role ({LEGACY_BACKUP_ROLE_NAME})"),
                &e,
            )),
        }
    }

    async fn delete_role(&self) -> Result<bool> {
        let spec = roles::backup_role(LEGACY_BACKUP_ROLE_NAME, None, false);
        crate::deploy::iam::delete_role(&self.iam, &spec.name, &spec.managed_policy_arns).await
    }
}

pub async fn run_cleanup_deprecated(
    stage: &str,
    delete_recovery_points: bool,
    dry_run: bool,
    yes: bool,
    sink: &mut dyn Sink,
) -> Result<()> {
    let (settings, advisories) = Settings::load(&find_toml_path()?, stage)?;
    for advisory in advisories {
        eprintln!("{}", advisory.message);
    }
    let region = settings.general.region.clone();
    let sdk = crate::aws_util::load_sdk().await;
    let config = regional_config(&sdk, &region);
    let api = AwsLegacy {
        backup: aws_sdk_backup::Client::new(&config),
        iam: aws_sdk_iam::Client::new(&config),
    };
    let options = LegacyOptions {
        delete_recovery_points,
        dry_run,
        vault_attempts: 12,
        vault_interval: Duration::from_secs(10),
    };
    let prompt = Interaction { assume_yes: yes };
    cleanup_deprecated(&api, &region, &options, &prompt, sink).await
}

#[cfg(test)]
mod tests {
    use std::cell::{Cell, RefCell};

    use aws_sdk_backup::primitives::DateTime;

    use super::*;
    use crate::aws_util::AwsError;
    use crate::deploy::interaction::testing::FakePrompt;
    use crate::deploy::poll::testing::run;
    use crate::status::sink::{Recorded, Style};

    fn point(arn: &str, vault: &str, delete_at: Option<i64>) -> RecoveryPoint {
        RecoveryPoint {
            arn: arn.to_string(),
            status: Some("COMPLETED".to_string()),
            creation_date: Some(DateTime::from_secs(1_763_600_523)),
            vault_name: Some(vault.to_string()),
            delete_at: delete_at.map(DateTime::from_secs),
        }
    }

    #[derive(Default)]
    struct FakeStage {
        points: Vec<(String, RecoveryPoint)>,
        denied: bool,
        deleted: RefCell<Vec<String>>,
    }

    impl StagePoints for FakeStage {
        async fn list(&self, resource_arn: &str) -> Result<Vec<RecoveryPoint>> {
            if self.denied {
                return Err(anyhow::Error::new(AwsError {
                    operation: "backup list_recovery_points_by_resource".to_string(),
                    code: "AccessDeniedException".to_string(),
                    message: "denied".to_string(),
                }));
            }
            Ok(self
                .points
                .iter()
                .filter(|(arn, _)| arn == resource_arn)
                .map(|(_, point)| point.clone())
                .collect())
        }

        async fn delete(&self, vault: &str, arn: &str) -> Result<()> {
            self.deleted.borrow_mut().push(format!("{vault} {arn}"));
            Ok(())
        }
    }

    fn texts(sink: &Recorded) -> Vec<&str> {
        sink.0.iter().map(|(_, _, text)| text.as_str()).collect()
    }

    #[test]
    fn test_cleanup_deletes_only_pocket_vault_points_after_confirmation() {
        let api = FakeStage {
            points: vec![
                ("arn:db".to_string(), point("arn:rp-1", "v", None)),
                ("arn:db".to_string(), point("arn:rp-user", "mine", None)),
                ("arn:other".to_string(), point("arn:rp-2", "v", None)),
            ],
            ..Default::default()
        };
        let targets = ["arn:db".to_string()];
        // 断られたら消さない
        let prompt = FakePrompt::answering(false);
        let mut sink = Recorded::default();
        let error = run(cleanup(&api, &targets, "v", &prompt, &mut sink)).unwrap_err();
        assert_eq!(error.to_string(), "Aborted!");
        assert!(api.deleted.borrow().is_empty());
        assert_eq!(
            *prompt.asked.borrow(),
            ["1 件の recovery point を削除しますか？"]
        );
        assert_eq!(
            texts(&sink),
            [
                "以下の recovery point を削除します:",
                "  - arn:rp-1 (2025-11-20T01:02:03+00:00)",
                "この操作は取り消せません！",
            ]
        );
        // -y なら聞かずに消す
        let mut sink = Recorded::default();
        run(cleanup(&api, &targets, "v", &FakePrompt::yes(), &mut sink)).unwrap();
        assert_eq!(*api.deleted.borrow(), ["v arn:rp-1"]);
        assert_eq!(
            texts(&sink)[3..],
            [
                "Deleted 1 recovery point(s).",
                "1 件の recovery point を削除しました。"
            ]
        );
    }

    #[test]
    fn test_cleanup_without_points_or_permission() {
        let api = FakeStage::default();
        let mut sink = Recorded::default();
        run(cleanup(&api, &[], "v", &FakePrompt::yes(), &mut sink)).unwrap();
        assert_eq!(texts(&sink), ["削除対象の recovery point はありません。"]);
        let api = FakeStage {
            denied: true,
            ..Default::default()
        };
        let error = run(cleanup(
            &api,
            &["arn:db".to_string()],
            "v",
            &FakePrompt::yes(),
            &mut sink,
        ))
        .unwrap_err();
        assert_eq!(
            error.to_string(),
            "recovery point を列挙する権限がありません (backup:*)。"
        );
    }

    #[derive(Default)]
    struct FakeLegacy {
        /// plan 名 → rule の vault
        plans: Vec<(String, Vec<String>)>,
        /// plan 名 → selection の role
        selections: Vec<(String, String)>,
        vault: bool,
        role: Cell<bool>,
        points: Vec<RecoveryPoint>,
        /// vault の削除が `false` (空でない) を返す回数
        vault_busy: Cell<u32>,
        calls: RefCell<Vec<String>>,
    }

    impl LegacyApi for FakeLegacy {
        async fn plans(&self) -> Result<Vec<(String, String)>> {
            Ok(self
                .plans
                .iter()
                .map(|(name, _)| (format!("id-{name}"), name.clone()))
                .collect())
        }

        async fn plan_vaults(&self, plan_id: &str) -> Result<Vec<String>> {
            Ok(self
                .plans
                .iter()
                .find(|(name, _)| format!("id-{name}") == plan_id)
                .map(|(_, vaults)| vaults.clone())
                .unwrap_or_default())
        }

        async fn selections(&self, plan_id: &str) -> Result<Vec<(String, String)>> {
            Ok(self
                .selections
                .iter()
                .filter(|(plan, _)| format!("id-{plan}") == plan_id)
                .map(|(plan, _)| (format!("sel-{plan}"), format!("{plan}-selection")))
                .collect())
        }

        async fn selection_role(
            &self,
            plan_id: &str,
            _selection_id: &str,
        ) -> Result<Option<String>> {
            Ok(self
                .selections
                .iter()
                .find(|(plan, _)| format!("id-{plan}") == plan_id)
                .map(|(_, role)| role.clone()))
        }

        async fn vault_exists(&self) -> Result<bool> {
            Ok(self.vault)
        }

        async fn vault_points(&self) -> Result<Vec<RecoveryPoint>> {
            Ok(self.points.clone())
        }

        async fn delete_point(&self, arn: &str) -> Result<()> {
            self.calls.borrow_mut().push(format!("delete_point {arn}"));
            Ok(())
        }

        async fn delete_vault(&self) -> Result<bool> {
            self.calls.borrow_mut().push("delete_vault".to_string());
            if self.vault_busy.get() > 0 {
                self.vault_busy.set(self.vault_busy.get() - 1);
                return Ok(false);
            }
            Ok(true)
        }

        async fn role_exists(&self) -> Result<bool> {
            Ok(self.role.get())
        }

        async fn delete_role(&self) -> Result<bool> {
            self.calls.borrow_mut().push("delete_role".to_string());
            Ok(true)
        }
    }

    fn options(delete_recovery_points: bool, dry_run: bool) -> LegacyOptions {
        LegacyOptions {
            delete_recovery_points,
            dry_run,
            vault_attempts: 3,
            vault_interval: Duration::ZERO,
        }
    }

    fn deprecated(
        api: &FakeLegacy,
        options: &LegacyOptions,
        prompt: &FakePrompt,
    ) -> (Result<()>, Recorded) {
        let mut sink = Recorded::default();
        let result = run(cleanup_deprecated(
            api,
            "ap-northeast-1",
            options,
            prompt,
            &mut sink,
        ));
        (result, sink)
    }

    #[test]
    fn test_cleanup_deprecated_aborts_while_referenced() {
        let api = FakeLegacy {
            plans: vec![
                (
                    "old-plan".to_string(),
                    vec![LEGACY_BACKUP_VAULT_NAME.to_string()],
                ),
                ("new-plan".to_string(), vec!["dev-app-backup".to_string()]),
            ],
            selections: vec![(
                "new-plan".to_string(),
                format!("arn:aws:iam::1:role/{LEGACY_BACKUP_ROLE_NAME}"),
            )],
            vault: true,
            ..Default::default()
        };
        let (result, sink) = deprecated(&api, &options(true, false), &FakePrompt::yes());
        assert!(result
            .unwrap_err()
            .to_string()
            .starts_with("旧リソースをまだ参照している設定があります (region: ap-northeast-1)。"));
        assert_eq!(
            texts(&sink),
            [
                "  - backup plan old-plan (保存先が pocket-backup)",
                "  - backup selection new-plan / new-plan-selection (ロールが forge-pocket-backup-role)",
            ]
        );
        assert!(api.calls.borrow().is_empty());
    }

    #[test]
    fn test_cleanup_deprecated_nothing_to_do_and_dry_run() {
        let api = FakeLegacy::default();
        let (result, sink) = deprecated(&api, &options(false, false), &FakePrompt::yes());
        result.unwrap();
        assert_eq!(
            texts(&sink),
            ["削除対象の旧リソースはありません (region: ap-northeast-1)。"]
        );
        let api = FakeLegacy {
            vault: true,
            role: Cell::new(true),
            ..Default::default()
        };
        let (result, sink) = deprecated(&api, &options(false, true), &FakePrompt::yes());
        result.unwrap();
        assert_eq!(
            texts(&sink),
            [
                "backup vault: pocket-backup (recovery point 0 件)",
                "IAM role: forge-pocket-backup-role"
            ]
        );
        assert!(api.calls.borrow().is_empty(), "dry-run は消さない");
    }

    #[test]
    fn test_cleanup_deprecated_keeps_vault_with_points_unless_asked() {
        let points = vec![
            point("arn:rp-1", LEGACY_BACKUP_VAULT_NAME, Some(1_763_600_523)),
            point("arn:rp-2", LEGACY_BACKUP_VAULT_NAME, Some(1_800_000_000)),
        ];
        let api = FakeLegacy {
            vault: true,
            role: Cell::new(true),
            points: points.clone(),
            ..Default::default()
        };
        // 消すのは role だけ。vault は失効日を案内して残す
        let prompt = FakePrompt::answering(true);
        let (result, sink) = deprecated(&api, &options(false, false), &prompt);
        result.unwrap();
        assert_eq!(*api.calls.borrow(), ["delete_role"]);
        assert_eq!(*prompt.asked.borrow(), ["上記の旧リソースを削除しますか？"]);
        let warning = sink.0.iter().find(|r| r.1 == Style::Warning).unwrap();
        assert!(warning.2.contains("(2027-01-15 以降)"), "{}", warning.2);

        // 無期限のものがあれば自動では空にならない
        let mut forever = points.clone();
        forever[1].delete_at = None;
        assert_eq!(last_expiry(&forever), None);

        // --delete-recovery-points: 件数を示して確認し、point → vault の順に消す。
        // vault の削除は反映待ちで再試行する
        let api = FakeLegacy {
            vault: true,
            points,
            vault_busy: Cell::new(2),
            ..Default::default()
        };
        let prompt = FakePrompt::answering(true);
        let (result, sink) = deprecated(&api, &options(true, false), &prompt);
        result.unwrap();
        assert!(
            prompt.asked.borrow()[0].starts_with("旧 vault の recovery point 2 件を削除します。")
        );
        assert_eq!(
            *api.calls.borrow(),
            [
                "delete_point arn:rp-1",
                "delete_point arn:rp-2",
                "delete_vault",
                "delete_vault",
                "delete_vault"
            ]
        );
        assert_eq!(
            texts(&sink).last(),
            Some(&"旧リソースの掃除が完了しました。")
        );

        // 反映されないまま回数が尽きたら止める
        let api = FakeLegacy {
            vault: true,
            vault_busy: Cell::new(5),
            ..Default::default()
        };
        let (result, _) = deprecated(&api, &options(false, false), &FakePrompt::yes());
        assert!(result
            .unwrap_err()
            .to_string()
            .starts_with("recovery point の削除がまだ反映されておらず"));

        // 確認を断れば何も消さない
        let api = FakeLegacy {
            vault: true,
            role: Cell::new(true),
            ..Default::default()
        };
        let (result, _) = deprecated(&api, &options(false, false), &FakePrompt::answering(false));
        assert_eq!(result.unwrap_err().to_string(), "Aborted!");
        assert!(api.calls.borrow().is_empty());
    }
}
