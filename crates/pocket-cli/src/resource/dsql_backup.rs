//! `pocket-rs resource dsql backup` / `backup-status` / `restore` / `restore-status`
//! (Python 版 `dsql_cli.py` と `resources/dsql.py` の backup / restore)。
//!
//! DSQL には組み込みの自動 backup が無く、AWS Backup の backup job が唯一の手段。restore は
//! 常に**新しい cluster** を作るので、`Name` タグを新しい cluster へ付け替えて現用を切り
//! 替える。付け替えと endpoint の公開は冪等で、待機が途中で止まっても `restore-status`
//! から続きを進められる。
//!
//! 切り替えの順 (Python 版と同じ):
//! 1. (`--skip-backup` でなければ) 現用 cluster の backup を取り、完了を待つ
//! 2. restore job を始め、完了を待つ
//! 3. 旧 cluster の `Name` を退避名にする (同じ名前の cluster が 2 つある瞬間を作らない)
//! 4. 新 cluster に `Name` を付ける
//! 5. endpoint を公開する
//!
//! 3 の後で止まると `Name` を持つ cluster が無くなるが、再実行 (`restore-status`) では
//! 旧 cluster が見つからないので 4 から進む

use std::cell::RefCell;

use anyhow::{anyhow, bail, Result};
use aws_config::SdkConfig;
use aws_sdk_backup::primitives::{DateTime, DateTimeFormat};
use magic_pocket_settings::context::DsqlContext;
use serde_json::json;

use super::DsqlBackupCommand;
use crate::aws_util::{error_code, regional_config, sdk_error, AwsError};
use crate::deploy::dsql::{endpoint, find_cluster, DsqlCluster};
use crate::deploy::interaction::{Interaction, Prompt};
use crate::deploy::poll::wait_until;
use crate::deploy::stored_value::{AwsStoredValues, StoredValues};
use crate::stack::ops::WaitOptions;
use crate::stack::roles::{self, RoleSpec};
use crate::status::sink::Sink;

/// backup job の終端状態。これ以外は進行中
const BACKUP_TERMINAL_STATES: [&str; 5] = ["COMPLETED", "FAILED", "ABORTED", "EXPIRED", "PARTIAL"];
/// restore job の終端状態
const RESTORE_TERMINAL_STATES: [&str; 3] = ["COMPLETED", "FAILED", "ABORTED"];
/// 保持日数の指定も `[backup.dsql]` も無いときの既定 (3 年)。vault は既定の lifecycle を
/// 持たないので、渡さないと recovery point は無期限に残る
pub const DEFAULT_ON_DEMAND_RETENTION_DAYS: i64 = 1095;

#[derive(Debug, Clone, PartialEq)]
pub struct BackupJob {
    pub id: String,
    pub state: String,
    pub percent_done: Option<String>,
    pub status_message: Option<String>,
    pub recovery_point_arn: Option<String>,
    pub creation_date: Option<DateTime>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct RestoreJob {
    pub status: String,
    pub status_message: Option<String>,
    pub created_resource_arn: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct RecoveryPoint {
    pub arn: String,
    pub status: Option<String>,
    pub creation_date: Option<DateTime>,
    pub vault_name: Option<String>,
    /// 失効の日時 (lifecycle が無ければ `None`)
    pub delete_at: Option<DateTime>,
}

/// backup / restore が呼ぶ AWS の API
pub trait DsqlBackupApi {
    /// `Name` タグが一致する cluster
    async fn find_cluster(&self, dsql: &DsqlContext) -> Result<Option<DsqlCluster>>;
    /// vault を作る。既に在れば `false`
    async fn create_vault(&self, name: &str) -> Result<bool>;
    async fn ensure_role(&self, spec: &RoleSpec) -> Result<String>;
    async fn start_backup_job(
        &self,
        vault: &str,
        resource_arn: &str,
        role_arn: &str,
        delete_after_days: Option<i64>,
    ) -> Result<String>;
    async fn backup_job(&self, job_id: &str) -> Result<BackupJob>;
    /// cluster を対象にした backup job (全ページ)
    async fn backup_jobs(&self, resource_arn: &str) -> Result<Vec<BackupJob>>;
    /// cluster の recovery point (全ページ)
    async fn recovery_points(&self, resource_arn: &str) -> Result<Vec<RecoveryPoint>>;
    /// `metadata` は `regionalConfig` の値 (JSON の文字列)
    async fn start_restore_job(
        &self,
        recovery_point_arn: &str,
        role_arn: &str,
        regional_config: &str,
    ) -> Result<String>;
    async fn restore_job(&self, job_id: &str) -> Result<RestoreJob>;
    /// cluster の `Name` タグを書く
    async fn tag_name(&self, resource_arn: &str, name: &str) -> Result<()>;
}

/// 待機の設定 (Python 版は backup も restore も 3600 秒を 15 秒おき)
#[derive(Debug, Clone, Copy)]
pub struct Waits {
    pub backup: WaitOptions,
    pub restore: WaitOptions,
}

impl Default for Waits {
    fn default() -> Self {
        Self {
            backup: WaitOptions::new(3600, 15),
            restore: WaitOptions::new(3600, 15),
        }
    }
}

pub struct DsqlBackup<'a, A, V> {
    pub api: &'a A,
    pub values: &'a V,
    pub dsql: &'a DsqlContext,
    pub waits: Waits,
}

/// AWS の error を Python 版の `"<code> (API: <operation>): <message>"` にする
/// (AWS の文言は API 名を含まないことがあるので、どの呼び出しで落ちたかを出す)
fn with_api_name(error: anyhow::Error) -> anyhow::Error {
    let Some(aws) = error.downcast_ref::<AwsError>() else {
        return error;
    };
    // 操作名は `backup start_backup_job` / `IAM get_role (name)` の形
    let operation = aws
        .operation
        .split(" (")
        .next()
        .unwrap_or_default()
        .rsplit(' ')
        .next()
        .unwrap_or_default();
    let pascal: String = operation
        .split('_')
        .map(|word| {
            let mut chars = word.chars();
            match chars.next() {
                Some(first) => first.to_uppercase().chain(chars).collect(),
                None => String::new(),
            }
        })
        .collect();
    anyhow!("{} (API: {pascal}): {}", aws.code, aws.message)
}

/// 先に現れた最大値 (Python の `max(key=...)` と同じく同値なら先のもの)
fn latest<T>(items: Vec<T>, key: impl Fn(&T) -> Option<DateTime>) -> Option<T> {
    let mut best: Option<(T, Option<DateTime>)> = None;
    for item in items {
        let date = key(&item);
        match &best {
            Some((_, current)) if date <= *current => {}
            _ => best = Some((item, date)),
        }
    }
    best.map(|(item, _)| item)
}

impl<A: DsqlBackupApi, V: StoredValues> DsqlBackup<'_, A, V> {
    fn role(&self) -> RoleSpec {
        roles::backup_role(
            &self.dsql.backup_role_name,
            self.dsql.permissions_boundary.as_deref(),
            self.dsql.external_roles,
        )
    }

    /// 保持日数: 指定 > `[backup.dsql]` の最長階層 (monthly) > 既定。`0` は無期限 (`None`)
    fn retention_days(&self, retention_days: Option<i64>, sink: &mut dyn Sink) -> Option<i64> {
        match retention_days {
            Some(0) => {
                sink.warning(
                    "この recovery point は無期限保持です。\
                     pocket からは削除できないため、消すには console 作業が必要です。",
                );
                None
            }
            Some(days) => {
                sink.log(&format!("Retention: {days} days (--retention-days)"));
                Some(days)
            }
            None => match self.dsql.backup.as_ref() {
                Some(plan) => {
                    let days = plan
                        .rules
                        .iter()
                        .map(|rule| rule.delete_after_days)
                        .max()
                        .unwrap_or_default();
                    sink.log(&format!(
                        "Retention: {days} days (from [backup.dsql] monthly)"
                    ));
                    Some(days)
                }
                None => {
                    sink.log(&format!(
                        "Retention: {DEFAULT_ON_DEMAND_RETENTION_DAYS} days (default)"
                    ));
                    Some(DEFAULT_ON_DEMAND_RETENTION_DAYS)
                }
            },
        }
    }

    /// on-demand の backup を始めて job id を返す。vault / role の指定が無ければ pocket が
    /// 管理するものを確保する (指定されたものは利用者の持ち物として存在を確かめない)
    pub async fn start_backup(
        &self,
        cluster: &DsqlCluster,
        vault: Option<&str>,
        iam_role_arn: Option<&str>,
        retention_days: Option<i64>,
        sink: &mut dyn Sink,
    ) -> Result<String> {
        let vault = match vault {
            Some(vault) => vault.to_string(),
            None => {
                let name = &self.dsql.backup_vault_name;
                if self.api.create_vault(name).await? {
                    sink.log(&format!("Created backup vault: {name}"));
                }
                name.clone()
            }
        };
        let role_arn = match iam_role_arn {
            Some(arn) => arn.to_string(),
            None => self.api.ensure_role(&self.role()).await?,
        };
        let days = self.retention_days(retention_days, sink);
        self.api
            .start_backup_job(&vault, &cluster.arn, &role_arn, days)
            .await
    }

    async fn wait_backup(&self, job_id: &str) -> Result<BackupJob> {
        let last = RefCell::new(None);
        let options = self.waits.backup;
        wait_until(
            options,
            "Waiting for backup job to complete",
            &format!(
                "Backup job did not complete within {} seconds",
                options.timeout_secs
            ),
            || async {
                let job = self.api.backup_job(job_id).await?;
                let done = BACKUP_TERMINAL_STATES.contains(&job.state.as_str());
                *last.borrow_mut() = Some(job);
                Ok(done)
            },
        )
        .await?;
        Ok(last.into_inner().expect("完了した job"))
    }

    async fn wait_restore(&self, job_id: &str) -> Result<RestoreJob> {
        let last = RefCell::new(None);
        let options = self.waits.restore;
        wait_until(
            options,
            "Waiting for restore job to complete",
            &format!(
                "Restore job did not complete within {} seconds",
                options.timeout_secs
            ),
            || async {
                let job = self.api.restore_job(job_id).await?;
                let done = RESTORE_TERMINAL_STATES.contains(&job.status.as_str());
                *last.borrow_mut() = Some(job);
                Ok(done)
            },
        )
        .await?;
        Ok(last.into_inner().expect("完了した job"))
    }

    /// `backup` コマンド
    pub async fn backup(
        &self,
        stage: &str,
        vault: Option<&str>,
        iam_role_arn: Option<&str>,
        retention_days: Option<i64>,
        watch: bool,
        sink: &mut dyn Sink,
    ) -> Result<()> {
        let Some(cluster) = self.api.find_cluster(self.dsql).await? else {
            bail!("Cluster not found");
        };
        let job_id = self
            .start_backup(&cluster, vault, iam_role_arn, retention_days, sink)
            .await
            .map_err(with_api_name)?;
        sink.success(&format!("Backup job started: {job_id}"));
        sink.info("Status check:");
        sink.info(&format!(
            "  pocket resource dsql backup-status --stage={stage} --job-id={job_id} --watch"
        ));
        sink.info(&format!(
            "  aws backup describe-backup-job --backup-job-id {job_id} --region {}",
            self.dsql.region
        ));
        if watch {
            self.finish_backup_watch(&job_id, sink).await?;
        }
        Ok(())
    }

    /// `backup-status` コマンド
    pub async fn backup_status(
        &self,
        job_id: Option<&str>,
        watch: bool,
        sink: &mut dyn Sink,
    ) -> Result<()> {
        let job = match job_id {
            Some(job_id) => self.api.backup_job(job_id).await?,
            None => {
                let cluster = self.api.find_cluster(self.dsql).await?;
                let jobs = match cluster {
                    Some(cluster) => self.api.backup_jobs(&cluster.arn).await?,
                    None => Vec::new(),
                };
                match latest(jobs, |job| job.creation_date) {
                    Some(job) => job,
                    None => bail!("No backup jobs found for this cluster"),
                }
            }
        };
        print_backup_job(&job, sink);
        if watch && !BACKUP_TERMINAL_STATES.contains(&job.state.as_str()) {
            self.finish_backup_watch(&job.id, sink).await?;
        }
        Ok(())
    }

    async fn finish_backup_watch(&self, job_id: &str, sink: &mut dyn Sink) -> Result<()> {
        let job = self.wait_backup(job_id).await?;
        if job.state == "COMPLETED" {
            sink.success(&format!(
                "Backup completed: {}",
                job.recovery_point_arn.as_deref().unwrap_or_default()
            ));
            return Ok(());
        }
        bail!(
            "Backup job finished with state {}: {}",
            job.state,
            job.status_message.as_deref().unwrap_or("(no message)")
        )
    }

    /// 復元元の recovery point の ARN
    async fn resolve_recovery_point(
        &self,
        cluster: Option<&DsqlCluster>,
        recovery_point_arn: Option<&str>,
        latest_flag: bool,
        sink: &mut dyn Sink,
    ) -> Result<String> {
        match (recovery_point_arn, latest_flag) {
            (Some(_), true) => bail!("recovery point の指定と --latest は同時に使えません"),
            (Some(arn), false) => return Ok(arn.to_string()),
            (None, false) => {
                bail!("復元元の recovery point ARN を指定するか --latest を付けてください")
            }
            (None, true) => {}
        }
        let points = match cluster {
            Some(cluster) => self.api.recovery_points(&cluster.arn).await?,
            None => Vec::new(),
        };
        let completed: Vec<RecoveryPoint> = points
            .into_iter()
            .filter(|point| point.status.as_deref() == Some("COMPLETED"))
            .collect();
        let Some(point) = latest(completed, |point| point.creation_date) else {
            bail!(
                "このクラスターの完了済みバックアップが見つかりません \
                 (クラスターが存在しない場合は recovery point ARN を直接指定してください)"
            );
        };
        sink.info(&format!(
            "Latest recovery point: {}",
            point.creation_date.map(py_datetime).unwrap_or_default()
        ));
        Ok(point.arn)
    }

    /// `restore` コマンド
    pub async fn restore(
        &self,
        stage: &str,
        recovery_point_arn: Option<&str>,
        latest_flag: bool,
        skip_backup: bool,
        prompt: &impl Prompt,
        sink: &mut dyn Sink,
    ) -> Result<()> {
        let cluster = self.api.find_cluster(self.dsql).await?;
        let recovery_point_arn = self
            .resolve_recovery_point(cluster.as_ref(), recovery_point_arn, latest_flag, sink)
            .await?;
        sink.info(&format!("Restore from: {recovery_point_arn}"));
        if let Some(cluster) = cluster.as_ref() {
            sink.info(&format!("Current cluster: {}", cluster.identifier));
        }
        if let Some(cluster) = cluster.as_ref().filter(|_| !skip_backup) {
            // 復元の結果が期待どおりでなかったときの唯一の戻り先なので、切り替えの前に
            // 現用の cluster を固めておく
            if prompt.confirm("Backup current cluster?", true)? {
                let job_id = self.start_backup(cluster, None, None, None, sink).await?;
                sink.log(&format!("Backup job started: {job_id}"));
                let job = self.wait_backup(&job_id).await?;
                if job.state != "COMPLETED" {
                    bail!(
                        "現用クラスターのバックアップが {} で終了しました: {}",
                        job.state,
                        job.status_message.as_deref().unwrap_or("(no message)")
                    );
                }
                sink.success("Current cluster backed up.");
            }
        }
        let role_arn = self.api.ensure_role(&self.role()).await?;
        let regional_config = json!([{
            "region": self.dsql.region,
            "isDeletionProtectionEnabled": self.dsql.deletion_protection,
        }]);
        let job_id = self
            .api
            .start_restore_job(
                &recovery_point_arn,
                &role_arn,
                &magic_pocket_settings::pyjson::dumps(&regional_config),
            )
            .await?;
        sink.success(&format!("Restore job started: {job_id}"));
        self.finish_restore(stage, &job_id, Some(cluster), sink)
            .await
    }

    /// `restore-status` コマンド
    pub async fn restore_status(
        &self,
        stage: &str,
        job_id: &str,
        sink: &mut dyn Sink,
    ) -> Result<()> {
        let job = self.api.restore_job(job_id).await?;
        sink.info(&format!("Job ID: {job_id}"));
        sink.info(&format!("Status: {}", job.status));
        if let Some(message) = job.status_message.as_deref().filter(|m| !m.is_empty()) {
            sink.info(&format!("Message: {message}"));
        }
        self.finish_restore(stage, job_id, None, sink).await
    }

    /// restore job の完了を待ち、現用の cluster を切り替える (冪等。途中で止まっても
    /// `restore-status` から再実行できる)。
    ///
    /// `known` は呼び出し側が既に引いた現用の cluster (`restore`)。`None` なら待機の後に引く
    /// (`restore-status`)
    async fn finish_restore(
        &self,
        stage: &str,
        job_id: &str,
        known: Option<Option<DsqlCluster>>,
        sink: &mut dyn Sink,
    ) -> Result<()> {
        let job = self.wait_restore(job_id).await?;
        if job.status != "COMPLETED" {
            bail!(
                "Restore job finished with status {}: {}",
                job.status,
                job.status_message.as_deref().unwrap_or("(no message)")
            );
        }
        let Some(new_arn) = job.created_resource_arn.filter(|arn| !arn.is_empty()) else {
            bail!("restore job は完了しましたが復元先 ARN を取得できませんでした: {job_id}");
        };
        let current = match known {
            Some(current) => current,
            None => self.api.find_cluster(self.dsql).await?,
        };
        let previous = current.as_ref().map(|c| c.identifier.clone());
        self.switch_to_cluster(current.as_ref(), &new_arn, sink)
            .await?;
        let Some(restored) = self.api.find_cluster(self.dsql).await? else {
            bail!(
                "復元先のクラスターが Name タグ ({}) で見つかりません: {new_arn}",
                self.dsql.tag_name
            );
        };
        self.publish_endpoint(&restored, sink).await?;
        sink.success(&format!(
            "Restored cluster is now current: {}",
            restored.identifier
        ));
        sink.success(&format!(
            "Endpoint: {}",
            endpoint(self.dsql, &restored.identifier)
        ));
        sink.warning(&format!(
            "まだアプリは切り替わっていません。`pocket deploy --stage={stage}` を実行するまで、\
             Lambda は旧クラスターに書き込み続けます \
             (endpoint と IAM ポリシーが CloudFormation 管理のため)。"
        ));
        if let Some(previous) = previous.filter(|p| *p != restored.identifier) {
            sink.warning(&format!(
                "旧クラスター {previous} は削除していません。明示的に削除するまで課金され続けます \
                 (戻り先として残しています)。"
            ));
        }
        Ok(())
    }

    /// `Name` タグを復元先へ付け替える。先に旧 cluster を退避名にして、同じ名前の cluster
    /// が 2 つある瞬間を作らない (pocket は Name で探すので、どちらを掴むか不定になる)
    async fn switch_to_cluster(
        &self,
        current: Option<&DsqlCluster>,
        new_arn: &str,
        sink: &mut dyn Sink,
    ) -> Result<()> {
        if let Some(old) = current.filter(|old| old.arn != new_arn) {
            let replaced = format!("{}-replaced-{}", self.dsql.tag_name, old.identifier);
            self.api.tag_name(&old.arn, &replaced).await?;
            sink.log(&format!("Renamed previous cluster: {}", old.identifier));
        }
        self.api.tag_name(new_arn, &self.dsql.tag_name).await
    }

    /// endpoint を正準名の stored secret に書く (値が同じなら書かない)
    async fn publish_endpoint(&self, cluster: &DsqlCluster, sink: &mut dyn Sink) -> Result<()> {
        let name = &self.dsql.endpoint_secret_name;
        if name.is_empty() {
            return Ok(());
        }
        let value = endpoint(self.dsql, &cluster.identifier);
        let (store, region) = (self.dsql.endpoint_secret_store, &self.dsql.region);
        if self.values.read(name, store, region).await?.as_deref() == Some(value.as_str()) {
            return Ok(());
        }
        self.values.put(name, store, &value, region).await?;
        sink.log(&format!("Published DSQL endpoint to {name}"));
        Ok(())
    }
}

fn print_backup_job(job: &BackupJob, sink: &mut dyn Sink) {
    sink.info(&format!("Job ID: {}", job.id));
    sink.info(&format!("State: {}", job.state));
    if let Some(percent) = job.percent_done.as_deref().filter(|p| !p.is_empty()) {
        sink.info(&format!("Progress: {percent}%"));
    }
    if let Some(message) = job.status_message.as_deref().filter(|m| !m.is_empty()) {
        sink.info(&format!("Message: {message}"));
    }
}

/// Python の `str(datetime)` (UTC の aware datetime): `2026-11-20 01:02:03.456000+00:00`
pub fn py_datetime(date: DateTime) -> String {
    py_iso(date).replacen('T', " ", 1)
}

/// Python の `datetime.isoformat()` (UTC の aware datetime):
/// `2026-11-20T01:02:03.456000+00:00` (秒未満が 0 なら省く)
pub fn py_iso(date: DateTime) -> String {
    let rfc = DateTime::from_secs(date.secs())
        .fmt(DateTimeFormat::DateTime)
        .unwrap_or_default();
    let base = rfc.trim_end_matches('Z');
    let micros = date.subsec_nanos() / 1000;
    if micros == 0 {
        format!("{base}+00:00")
    } else {
        format!("{base}.{micros:06}+00:00")
    }
}

// ---- AWS ----

pub struct AwsDsqlBackup<'a> {
    pub sdk: &'a SdkConfig,
    pub region: &'a str,
}

impl AwsDsqlBackup<'_> {
    fn backup(&self) -> aws_sdk_backup::Client {
        aws_sdk_backup::Client::new(&regional_config(self.sdk, self.region))
    }
}

fn backup_job_of(job: &aws_sdk_backup::types::BackupJob) -> BackupJob {
    BackupJob {
        id: job.backup_job_id().unwrap_or_default().to_string(),
        state: job
            .state()
            .map(|s| s.as_str().to_string())
            .unwrap_or_default(),
        percent_done: job.percent_done().map(str::to_string),
        status_message: job.status_message().map(str::to_string),
        recovery_point_arn: job.recovery_point_arn().map(str::to_string),
        creation_date: job.creation_date().copied(),
    }
}

impl DsqlBackupApi for AwsDsqlBackup<'_> {
    async fn find_cluster(&self, dsql: &DsqlContext) -> Result<Option<DsqlCluster>> {
        find_cluster(self.sdk, &dsql.region, &dsql.tag_name).await
    }

    async fn create_vault(&self, name: &str) -> Result<bool> {
        use crate::deploy::backup::{AwsBackup, BackupApi};
        AwsBackup {
            sdk: self.sdk,
            region: self.region,
        }
        .create_vault(name)
        .await
    }

    async fn ensure_role(&self, spec: &RoleSpec) -> Result<String> {
        let iam = aws_sdk_iam::Client::new(&regional_config(self.sdk, self.region));
        crate::deploy::iam::ensure_role(&iam, spec).await
    }

    async fn start_backup_job(
        &self,
        vault: &str,
        resource_arn: &str,
        role_arn: &str,
        delete_after_days: Option<i64>,
    ) -> Result<String> {
        let mut request = self
            .backup()
            .start_backup_job()
            .backup_vault_name(vault)
            .resource_arn(resource_arn)
            .iam_role_arn(role_arn);
        if let Some(days) = delete_after_days {
            request = request.lifecycle(
                aws_sdk_backup::types::Lifecycle::builder()
                    .delete_after_days(days)
                    .build(),
            );
        }
        let response = request
            .send()
            .await
            .map_err(|e| sdk_error("backup start_backup_job", &e))?;
        response
            .backup_job_id()
            .map(str::to_string)
            .ok_or_else(|| anyhow!("backup start_backup_job が job の id を返しませんでした"))
    }

    async fn backup_job(&self, job_id: &str) -> Result<BackupJob> {
        let job = self
            .backup()
            .describe_backup_job()
            .backup_job_id(job_id)
            .send()
            .await
            .map_err(|e| sdk_error("backup describe_backup_job", &e))?;
        Ok(BackupJob {
            id: job.backup_job_id().unwrap_or(job_id).to_string(),
            state: job
                .state()
                .map(|s| s.as_str().to_string())
                .unwrap_or_default(),
            percent_done: job.percent_done().map(str::to_string),
            status_message: job.status_message().map(str::to_string),
            recovery_point_arn: job.recovery_point_arn().map(str::to_string),
            creation_date: job.creation_date().copied(),
        })
    }

    async fn backup_jobs(&self, resource_arn: &str) -> Result<Vec<BackupJob>> {
        let mut pages = self
            .backup()
            .list_backup_jobs()
            .by_resource_arn(resource_arn)
            .into_paginator()
            .send();
        let mut jobs = Vec::new();
        while let Some(page) = pages.next().await {
            let page = page.map_err(|e| sdk_error("backup list_backup_jobs", &e))?;
            jobs.extend(page.backup_jobs().iter().map(backup_job_of));
        }
        Ok(jobs)
    }

    async fn recovery_points(&self, resource_arn: &str) -> Result<Vec<RecoveryPoint>> {
        list_recovery_points_by_resource(&self.backup(), resource_arn).await
    }

    async fn start_restore_job(
        &self,
        recovery_point_arn: &str,
        role_arn: &str,
        regional_config: &str,
    ) -> Result<String> {
        let response = self
            .backup()
            .start_restore_job()
            .recovery_point_arn(recovery_point_arn)
            .iam_role_arn(role_arn)
            .metadata("regionalConfig", regional_config)
            .send()
            .await
            .map_err(|e| sdk_error("backup start_restore_job", &e))?;
        response
            .restore_job_id()
            .map(str::to_string)
            .ok_or_else(|| anyhow!("backup start_restore_job が job の id を返しませんでした"))
    }

    async fn restore_job(&self, job_id: &str) -> Result<RestoreJob> {
        let job = self
            .backup()
            .describe_restore_job()
            .restore_job_id(job_id)
            .send()
            .await
            .map_err(|e| sdk_error("backup describe_restore_job", &e))?;
        Ok(RestoreJob {
            status: job
                .status()
                .map(|s| s.as_str().to_string())
                .unwrap_or_default(),
            status_message: job.status_message().map(str::to_string),
            created_resource_arn: job.created_resource_arn().map(str::to_string),
        })
    }

    async fn tag_name(&self, resource_arn: &str, name: &str) -> Result<()> {
        aws_sdk_dsql::Client::new(&regional_config(self.sdk, self.region))
            .tag_resource()
            .resource_arn(resource_arn)
            .tags("Name", name)
            .send()
            .await
            .map_err(|e| sdk_error("dsql tag_resource", &e))?;
        Ok(())
    }
}

/// resource の recovery point (全ページ)。`backup cleanup` も使う
pub async fn list_recovery_points_by_resource(
    client: &aws_sdk_backup::Client,
    resource_arn: &str,
) -> Result<Vec<RecoveryPoint>> {
    let mut pages = client
        .list_recovery_points_by_resource()
        .resource_arn(resource_arn)
        .into_paginator()
        .send();
    let mut points = Vec::new();
    while let Some(page) = pages.next().await {
        let page = page.map_err(|e| sdk_error("backup list_recovery_points_by_resource", &e))?;
        points.extend(page.recovery_points().iter().map(|point| RecoveryPoint {
            arn: point.recovery_point_arn().unwrap_or_default().to_string(),
            status: point.status().map(|s| s.as_str().to_string()),
            creation_date: point.creation_date().copied(),
            vault_name: point.backup_vault_name().map(str::to_string),
            delete_at: None,
        }));
    }
    Ok(points)
}

/// AccessDenied で落ちたか (`backup cleanup` の案内に使う)
pub fn is_access_denied(error: &anyhow::Error) -> bool {
    error_code(error) == Some("AccessDeniedException")
}

pub async fn run(stage: &str, command: DsqlBackupCommand, sink: &mut dyn Sink) -> Result<()> {
    let (context, _) = crate::context::load_context(stage)?;
    let Some(dsql) = context.dsql.as_ref() else {
        bail!("dsql is not configured for this stage");
    };
    let sdk = crate::aws_util::load_sdk().await;
    let api = AwsDsqlBackup {
        sdk: &sdk,
        region: &dsql.region,
    };
    let values = AwsStoredValues { sdk: &sdk };
    let ops = DsqlBackup {
        api: &api,
        values: &values,
        dsql,
        waits: Waits::default(),
    };
    match command {
        DsqlBackupCommand::Backup {
            vault,
            iam_role_arn,
            retention_days,
            watch,
        } => {
            ops.backup(
                stage,
                vault.as_deref(),
                iam_role_arn.as_deref(),
                retention_days,
                watch,
                sink,
            )
            .await
        }
        DsqlBackupCommand::BackupStatus { job_id, watch } => {
            ops.backup_status(job_id.as_deref(), watch, sink).await
        }
        DsqlBackupCommand::Restore {
            recovery_point_arn,
            latest,
            yes,
            skip_backup,
        } => {
            let prompt = Interaction { assume_yes: yes };
            ops.restore(
                stage,
                recovery_point_arn.as_deref(),
                latest,
                skip_backup,
                &prompt,
                sink,
            )
            .await
        }
        DsqlBackupCommand::RestoreStatus { job_id } => {
            ops.restore_status(stage, &job_id, sink).await
        }
    }
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;

    use super::*;
    use crate::deploy::interaction::testing::FakePrompt;
    use crate::deploy::poll::testing::{fast, run};
    use crate::deploy::stored_value::fake::FakeStoredValues;
    use crate::stack::testing::vector_context;
    use crate::status::sink::{Recorded, Style};

    /// cluster は (identifier, `Name` タグ)。ARN は `arn:<identifier>`
    #[derive(Default)]
    struct FakeApi {
        clusters: RefCell<Vec<(String, String)>>,
        /// backup job の最終状態 (既定は COMPLETED)
        backup_state: Option<&'static str>,
        /// restore job の最終状態 (既定は COMPLETED)
        restore_state: Option<&'static str>,
        jobs: Vec<BackupJob>,
        points: Vec<RecoveryPoint>,
        /// この呼び出しを 1 回だけ失敗させる (`tag <arn>` / `start_restore` など)
        fail_once: RefCell<Option<String>>,
        calls: RefCell<Vec<String>>,
    }

    impl FakeApi {
        fn with_cluster(identifier: &str, name: &str) -> Self {
            let api = Self::default();
            api.clusters
                .borrow_mut()
                .push((identifier.to_string(), name.to_string()));
            api
        }

        fn call(&self, line: String) -> Result<()> {
            if self.fail_once.borrow().as_deref() == Some(line.as_str()) {
                self.fail_once.borrow_mut().take();
                bail!("injected failure: {line}");
            }
            self.calls.borrow_mut().push(line);
            Ok(())
        }

        fn calls(&self) -> Vec<String> {
            self.calls.borrow().clone()
        }

        fn names(&self) -> Vec<(String, String)> {
            self.clusters.borrow().clone()
        }
    }

    impl DsqlBackupApi for FakeApi {
        async fn find_cluster(&self, dsql: &DsqlContext) -> Result<Option<DsqlCluster>> {
            Ok(self
                .clusters
                .borrow()
                .iter()
                .find(|(_, name)| *name == dsql.tag_name)
                .map(|(identifier, _)| DsqlCluster {
                    identifier: identifier.clone(),
                    arn: format!("arn:{identifier}"),
                    status: "ACTIVE".to_string(),
                }))
        }

        async fn create_vault(&self, name: &str) -> Result<bool> {
            self.call(format!("vault {name}"))?;
            Ok(false)
        }

        async fn ensure_role(&self, spec: &RoleSpec) -> Result<String> {
            self.call(format!("role {}", spec.name))?;
            Ok(format!("arn:role/{}", spec.name))
        }

        async fn start_backup_job(
            &self,
            vault: &str,
            resource_arn: &str,
            _role_arn: &str,
            delete_after_days: Option<i64>,
        ) -> Result<String> {
            self.call(format!(
                "start_backup {vault} {resource_arn} {delete_after_days:?}"
            ))?;
            Ok("bj-1".to_string())
        }

        async fn backup_job(&self, job_id: &str) -> Result<BackupJob> {
            Ok(BackupJob {
                id: job_id.to_string(),
                state: self.backup_state.unwrap_or("COMPLETED").to_string(),
                percent_done: None,
                status_message: Some("why".to_string()),
                recovery_point_arn: Some("arn:rp-new".to_string()),
                creation_date: None,
            })
        }

        async fn backup_jobs(&self, _resource_arn: &str) -> Result<Vec<BackupJob>> {
            Ok(self.jobs.clone())
        }

        async fn recovery_points(&self, _resource_arn: &str) -> Result<Vec<RecoveryPoint>> {
            Ok(self.points.clone())
        }

        async fn start_restore_job(
            &self,
            recovery_point_arn: &str,
            _role_arn: &str,
            regional_config: &str,
        ) -> Result<String> {
            self.call(format!(
                "start_restore {recovery_point_arn} {regional_config}"
            ))?;
            Ok("rj-1".to_string())
        }

        async fn restore_job(&self, _job_id: &str) -> Result<RestoreJob> {
            let status = self.restore_state.unwrap_or("COMPLETED");
            if status == "COMPLETED" && !self.clusters.borrow().iter().any(|(i, _)| i == "new") {
                // 復元先の cluster は Name タグ無しで現れる
                self.clusters
                    .borrow_mut()
                    .push(("new".to_string(), String::new()));
            }
            Ok(RestoreJob {
                status: status.to_string(),
                status_message: None,
                created_resource_arn: (status == "COMPLETED").then(|| "arn:new".to_string()),
            })
        }

        async fn tag_name(&self, resource_arn: &str, name: &str) -> Result<()> {
            self.call(format!("tag {resource_arn}"))?;
            let identifier = resource_arn.trim_start_matches("arn:");
            for cluster in self.clusters.borrow_mut().iter_mut() {
                if cluster.0 == identifier {
                    cluster.1 = name.to_string();
                }
            }
            Ok(())
        }
    }

    fn dsql() -> DsqlContext {
        vector_context("toml_vectors/db_backup.dev")
            .dsql
            .expect("dsql")
    }

    fn ops<'a>(
        api: &'a FakeApi,
        values: &'a FakeStoredValues,
        dsql: &'a DsqlContext,
    ) -> DsqlBackup<'a, FakeApi, FakeStoredValues> {
        DsqlBackup {
            api,
            values,
            dsql,
            waits: Waits {
                backup: fast(3),
                restore: fast(3),
            },
        }
    }

    fn date(secs: i64) -> Option<DateTime> {
        Some(DateTime::from_secs(secs))
    }

    fn texts(sink: &Recorded) -> Vec<String> {
        sink.0.iter().map(|(_, _, text)| text.clone()).collect()
    }

    #[test]
    fn test_backup_uses_pocket_vault_and_retention_rules() {
        let mut dsql = vector_context("toml_vectors/db_backup.prod")
            .dsql
            .expect("dsql");
        let api = FakeApi::with_cluster("c1", &dsql.tag_name);
        let values = FakeStoredValues::default();
        let mut sink = Recorded::default();
        // 既定: [backup.dsql] の最長階層
        let monthly = dsql
            .backup
            .as_ref()
            .and_then(|plan| plan.rules.iter().map(|r| r.delete_after_days).max())
            .expect("backup の宣言");
        run(ops(&api, &values, &dsql).backup("dev", None, None, None, false, &mut sink))
            .expect("backup");
        assert_eq!(
            api.calls(),
            [
                format!("vault {}", dsql.backup_vault_name),
                format!("role {}", dsql.backup_role_name),
                format!(
                    "start_backup {} arn:c1 Some({monthly})",
                    dsql.backup_vault_name
                ),
            ]
        );
        assert!(texts(&sink).contains(&format!(
            "Retention: {monthly} days (from [backup.dsql] monthly)"
        )));
        assert!(texts(&sink).contains(&"Backup job started: bj-1".to_string()));

        // 宣言が無ければ 1095 日。指定した vault / role は確保しない
        dsql.backup = None;
        api.calls.borrow_mut().clear();
        let mut sink = Recorded::default();
        run(ops(&api, &values, &dsql).backup(
            "dev",
            Some("mine"),
            Some("arn:role/mine"),
            None,
            false,
            &mut sink,
        ))
        .expect("backup");
        assert_eq!(api.calls(), ["start_backup mine arn:c1 Some(1095)"]);

        // 0 は無期限 (lifecycle を渡さない) で、警告を出す
        api.calls.borrow_mut().clear();
        let mut sink = Recorded::default();
        run(ops(&api, &values, &dsql).backup("dev", Some("mine"), None, Some(0), false, &mut sink))
            .expect("backup");
        assert_eq!(api.calls().last().unwrap(), "start_backup mine arn:c1 None");
        assert!(sink.0.iter().any(|r| r.1 == Style::Warning));
    }

    #[test]
    fn test_backup_errors() {
        let dsql = dsql();
        let api = FakeApi::default();
        let values = FakeStoredValues::default();
        let error = run(ops(&api, &values, &dsql).backup(
            "dev",
            None,
            None,
            None,
            false,
            &mut Recorded::default(),
        ))
        .unwrap_err();
        assert_eq!(error.to_string(), "Cluster not found");

        // AWS の error は API 名を添える
        let aws = anyhow::Error::new(AwsError {
            operation: "backup start_backup_job".to_string(),
            code: "AccessDeniedException".to_string(),
            message: "Insufficient privileges".to_string(),
        });
        assert_eq!(
            with_api_name(aws).to_string(),
            "AccessDeniedException (API: StartBackupJob): Insufficient privileges"
        );
        let iam = anyhow::Error::new(AwsError {
            operation: "IAM get_role (x-role)".to_string(),
            code: "AccessDenied".to_string(),
            message: "no".to_string(),
        });
        assert_eq!(
            with_api_name(iam).to_string(),
            "AccessDenied (API: GetRole): no"
        );

        // watch で終端が COMPLETED 以外なら止める
        let api = FakeApi {
            backup_state: Some("FAILED"),
            ..FakeApi::with_cluster("c1", &dsql.tag_name)
        };
        let error = run(ops(&api, &values, &dsql).backup(
            "dev",
            None,
            None,
            None,
            true,
            &mut Recorded::default(),
        ))
        .unwrap_err();
        assert_eq!(
            error.to_string(),
            "Backup job finished with state FAILED: why"
        );
    }

    #[test]
    fn test_backup_status_picks_latest_job() {
        let dsql = dsql();
        let job = |id: &str, secs: i64| BackupJob {
            id: id.to_string(),
            state: "RUNNING".to_string(),
            percent_done: Some("42.0".to_string()),
            status_message: None,
            recovery_point_arn: None,
            creation_date: date(secs),
        };
        let api = FakeApi {
            jobs: vec![job("old", 1), job("newest", 3), job("mid", 2)],
            ..FakeApi::with_cluster("c1", &dsql.tag_name)
        };
        let values = FakeStoredValues::default();
        let mut sink = Recorded::default();
        run(ops(&api, &values, &dsql).backup_status(None, false, &mut sink)).expect("status");
        assert_eq!(
            texts(&sink),
            ["Job ID: newest", "State: RUNNING", "Progress: 42.0%"]
        );
        let api = FakeApi::with_cluster("c1", &dsql.tag_name);
        let error =
            run(ops(&api, &values, &dsql).backup_status(None, false, &mut sink)).unwrap_err();
        assert_eq!(error.to_string(), "No backup jobs found for this cluster");
    }

    #[test]
    fn test_resolve_recovery_point() {
        let dsql = dsql();
        let point = |arn: &str, status: &str, secs: i64| RecoveryPoint {
            arn: arn.to_string(),
            status: Some(status.to_string()),
            creation_date: date(secs),
            vault_name: None,
            delete_at: None,
        };
        let api = FakeApi {
            points: vec![
                point("arn:rp-1", "COMPLETED", 10),
                point("arn:rp-partial", "PARTIAL", 30),
                point("arn:rp-2", "COMPLETED", 20),
            ],
            ..FakeApi::with_cluster("c1", &dsql.tag_name)
        };
        let values = FakeStoredValues::default();
        let o = ops(&api, &values, &dsql);
        let cluster = run(api.find_cluster(&dsql)).unwrap();
        let mut sink = Recorded::default();
        let arn = run(o.resolve_recovery_point(cluster.as_ref(), None, true, &mut sink)).unwrap();
        assert_eq!(arn, "arn:rp-2");
        assert_eq!(
            texts(&sink),
            ["Latest recovery point: 1970-01-01 00:00:20+00:00"]
        );
        for (arn, latest, message) in [
            (
                Some("arn:x"),
                true,
                "recovery point の指定と --latest は同時に使えません",
            ),
            (
                None,
                false,
                "復元元の recovery point ARN を指定するか --latest を付けてください",
            ),
        ] {
            let error = run(o.resolve_recovery_point(cluster.as_ref(), arn, latest, &mut sink))
                .unwrap_err();
            assert_eq!(error.to_string(), message);
        }
        // cluster が無ければ --latest は使えない
        let error = run(o.resolve_recovery_point(None, None, true, &mut sink)).unwrap_err();
        assert!(error
            .to_string()
            .starts_with("このクラスターの完了済みバックアップ"));
    }

    fn restore(
        api: &FakeApi,
        values: &FakeStoredValues,
        dsql: &DsqlContext,
        skip_backup: bool,
    ) -> Result<Recorded> {
        let mut sink = Recorded::default();
        run(ops(api, values, dsql).restore(
            "dev",
            Some("arn:rp-1"),
            false,
            skip_backup,
            &FakePrompt::yes(),
            &mut sink,
        ))?;
        Ok(sink)
    }

    fn restore_status(
        api: &FakeApi,
        values: &FakeStoredValues,
        dsql: &DsqlContext,
    ) -> Result<Recorded> {
        let mut sink = Recorded::default();
        run(ops(api, values, dsql).restore_status("dev", "rj-1", &mut sink))?;
        Ok(sink)
    }

    /// 切り替え後の状態: 新 cluster が現用、旧 cluster は退避名で残る、endpoint は新
    fn assert_switched(api: &FakeApi, values: &FakeStoredValues, dsql: &DsqlContext) {
        let replaced = format!("{}-replaced-c1", dsql.tag_name);
        assert_eq!(
            api.names(),
            [
                ("c1".to_string(), replaced),
                ("new".to_string(), dsql.tag_name.clone())
            ]
        );
        assert_eq!(
            values
                .get(dsql.endpoint_secret_store, &dsql.endpoint_secret_name)
                .as_deref(),
            Some(endpoint(dsql, "new").as_str())
        );
    }

    #[test]
    fn test_restore_backs_up_then_switches_in_order() {
        let dsql = dsql();
        let api = FakeApi::with_cluster("c1", &dsql.tag_name);
        let values = FakeStoredValues::default();
        let sink = restore(&api, &values, &dsql, false).expect("restore");
        let calls = api.calls();
        let restore_line = calls
            .iter()
            .position(|c| c.starts_with("start_restore"))
            .unwrap();
        assert!(calls[..restore_line]
            .iter()
            .any(|c| c.starts_with("start_backup")));
        assert_eq!(
            calls[restore_line],
            format!(
                "start_restore arn:rp-1 [{{\"region\": \"{}\", \"isDeletionProtectionEnabled\": {}}}]",
                dsql.region, dsql.deletion_protection
            )
        );
        // 旧 cluster の退避名 → 新 cluster の Name の順 (同じ名前が 2 つある瞬間を作らない)
        assert_eq!(&calls[restore_line + 1..], ["tag arn:c1", "tag arn:new"]);
        assert_switched(&api, &values, &dsql);
        let lines = texts(&sink);
        assert!(lines.contains(&"Current cluster backed up.".to_string()));
        assert!(lines.contains(&"Restored cluster is now current: new".to_string()));
        assert!(lines
            .iter()
            .any(|l| l.starts_with("旧クラスター c1 は削除していません")));

        // --skip-backup なら現用の backup を取らない
        let api = FakeApi::with_cluster("c1", &dsql.tag_name);
        restore(&api, &values, &dsql, true).expect("restore");
        assert!(!api.calls().iter().any(|c| c.starts_with("start_backup")));
    }

    #[test]
    fn test_restore_stops_before_switching_when_backup_or_job_fails() {
        let dsql = dsql();
        let values = FakeStoredValues::default();
        // 現用の backup が完了しない → restore を始めない
        let api = FakeApi {
            backup_state: Some("FAILED"),
            ..FakeApi::with_cluster("c1", &dsql.tag_name)
        };
        let error = restore(&api, &values, &dsql, false).unwrap_err();
        assert_eq!(
            error.to_string(),
            "現用クラスターのバックアップが FAILED で終了しました: why"
        );
        assert!(!api.calls().iter().any(|c| c.starts_with("start_restore")));
        assert_eq!(api.names(), [("c1".to_string(), dsql.tag_name.clone())]);

        // restore の開始が失敗 → 何も付け替えない
        let api = FakeApi::with_cluster("c1", &dsql.tag_name);
        *api.fail_once.borrow_mut() = Some(format!(
            "start_restore arn:rp-1 [{{\"region\": \"{}\", \"isDeletionProtectionEnabled\": {}}}]",
            dsql.region, dsql.deletion_protection
        ));
        restore(&api, &values, &dsql, true).unwrap_err();
        assert_eq!(api.names(), [("c1".to_string(), dsql.tag_name.clone())]);

        // restore job が失敗 → 何も付け替えない
        let api = FakeApi {
            restore_state: Some("FAILED"),
            ..FakeApi::with_cluster("c1", &dsql.tag_name)
        };
        let error = restore(&api, &values, &dsql, true).unwrap_err();
        assert_eq!(
            error.to_string(),
            "Restore job finished with status FAILED: (no message)"
        );
        assert!(!api.calls().iter().any(|c| c.starts_with("tag")));
        assert!(values.calls().is_empty());
    }

    #[test]
    fn test_restore_status_resumes_after_each_switch_step() {
        let dsql = dsql();
        // 旧 cluster の改名で失敗 → 再実行で改名から
        // 新 cluster の Name 付けで失敗 (旧は改名済み = Name を持つ cluster が無い) →
        // 再実行では旧 cluster が見つからないので、Name 付けから
        for failing in ["tag arn:c1", "tag arn:new"] {
            let api = FakeApi::with_cluster("c1", &dsql.tag_name);
            let values = FakeStoredValues::default();
            *api.fail_once.borrow_mut() = Some(failing.to_string());
            let error = restore(&api, &values, &dsql, true).unwrap_err();
            assert_eq!(error.to_string(), format!("injected failure: {failing}"));
            assert!(
                values.calls().is_empty(),
                "切り替え前に endpoint を書かない"
            );
            restore_status(&api, &values, &dsql).expect("resume");
            assert_switched(&api, &values, &dsql);
        }

        // endpoint の公開で失敗 → 再実行では付け替えずに公開だけ
        let api = FakeApi::with_cluster("c1", &dsql.tag_name);
        let values = FakeStoredValues::default();
        values.fail_put.set(true);
        restore(&api, &values, &dsql, true).unwrap_err();
        values.fail_put.set(false);
        let before = api.calls().len();
        let sink = restore_status(&api, &values, &dsql).expect("resume");
        assert_eq!(api.calls()[before..], ["tag arn:new"]);
        assert_switched(&api, &values, &dsql);
        let lines = texts(&sink);
        assert_eq!(lines[..2], ["Job ID: rj-1", "Status: COMPLETED"]);
        // 切り替え済みなので旧 cluster の案内は出ない
        assert!(!lines.iter().any(|l| l.starts_with("旧クラスター")));

        // 完了後の再実行も冪等 (公開済みなら書かない)
        let puts = values.calls().len();
        restore_status(&api, &values, &dsql).expect("again");
        assert_eq!(values.calls().len(), puts);
        assert_switched(&api, &values, &dsql);
    }

    #[test]
    fn test_py_datetime_formats() {
        let at = DateTime::from_secs_and_nanos(1_763_600_523, 456_000_000);
        assert_eq!(py_iso(at), "2025-11-20T01:02:03.456000+00:00");
        assert_eq!(py_datetime(at), "2025-11-20 01:02:03.456000+00:00");
        assert_eq!(
            py_iso(DateTime::from_secs(1_763_600_523)),
            "2025-11-20T01:02:03+00:00"
        );
    }
}
