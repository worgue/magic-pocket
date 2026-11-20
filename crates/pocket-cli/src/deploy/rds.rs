//! Aurora PostgreSQL (Serverless v2) の cluster の手順。Python 版
//! `pocket_cli/resources/rds.py` の deploy_init / create / update / delete。
//!
//! create の各段は「既に在れば再利用」で冪等にしてある (途中で失敗した deploy を
//! やり直せる)。AWS の呼び出しは [`RdsAws`] / [`Stacks`] / [`StoredValues`] の裏に置き、
//! 段の順序と分岐を fake でテストする。状態の判定は status と同じ関数を使う。

use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{anyhow, bail, Context as _, Result};
use aws_config::SdkConfig;
use magic_pocket_settings::context::{RdsContext, VpcContext};
use magic_pocket_settings::model::{PasswordStrategy, StoreType};
use magic_pocket_settings::pyjson;
use serde_json::{json, Map, Value as Json};

use super::generate::master_password;
use super::poll::wait_until;
use super::stacks::{self, Stacks};
use super::stored_value::{PutResult, StoredValues};
use crate::aws_util::{has_code, regional_config, sdk_error};
use crate::stack::ops::{cfn_status, stack_outputs, ResourceStatus, WaitOptions};
use crate::stack::vpc as vpc_stack;
use crate::status::rds::{self as rds_status, ClusterState};
use crate::status::sink::Sink;

/// RDS の手順が使う AWS の操作 (region は実装が持つ)
pub trait RdsAws {
    async fn cluster(&self, identifier: &str) -> Result<Option<ClusterState>>;
    /// instance の状態 (`available` など)。無ければ `None`
    async fn instance_status(&self, identifier: &str) -> Result<Option<String>>;
    /// subnet group を作る。既に在れば `false`
    async fn create_subnet_group(&self, args: &Json) -> Result<bool>;
    /// subnet group を消す。無ければ `false`
    async fn delete_subnet_group(&self, name: &str) -> Result<bool>;
    /// `Name` タグが `name` の security group の id
    async fn security_group_id(&self, name: &str) -> Result<Option<String>>;
    async fn create_security_group(&self, args: &Json) -> Result<String>;
    async fn delete_security_group(&self, group_id: &str) -> Result<()>;
    async fn create_cluster(&self, args: &Json) -> Result<()>;
    async fn restore_cluster(&self, args: &Json) -> Result<()>;
    async fn create_instance(&self, args: &Json) -> Result<()>;
    async fn modify_cluster(&self, args: &Json) -> Result<()>;
    async fn delete_instance(&self, identifier: &str) -> Result<()>;
    /// 最終 snapshot を取って cluster を消す
    async fn delete_cluster(&self, identifier: &str, final_snapshot: &str) -> Result<()>;
    /// static の認証情報を入れた secret (Secrets Manager) の ARN
    async fn static_secret_arn(&self, name: &str) -> Result<Option<String>>;
    async fn ssm_parameter_exists(&self, name: &str) -> Result<bool>;
    /// secret の今の `RotationRules` (比べる項目だけ)
    async fn rotation_rules(&self, secret_arn: &str) -> Result<Map<String, Json>>;
    /// rotation の窓を設定する (今すぐの rotation はしない)
    async fn rotate_secret(&self, secret_arn: &str, rules: &Map<String, Json>) -> Result<()>;
}

// ---- API に渡す引数 (boto3 の形。vector で Python と一致) ----

fn name_tags(name: &str) -> Json {
    json!([{"Key": "Name", "Value": name}])
}

pub fn subnet_group_args(rds: &RdsContext, subnet_ids: &[String]) -> Json {
    json!({
        "DBSubnetGroupName": rds.subnet_group_name,
        "DBSubnetGroupDescription": format!("Aurora subnet group for {}", rds.cluster_identifier),
        "SubnetIds": subnet_ids,
        "Tags": name_tags(&rds.subnet_group_name),
    })
}

pub fn security_group_args(rds: &RdsContext, vpc_id: &str) -> Json {
    json!({
        "GroupName": rds.security_group_name,
        "Description": format!("RDS Aurora security group for {}", rds.cluster_identifier),
        "VpcId": vpc_id,
        "TagSpecifications": [{
            "ResourceType": "security-group",
            "Tags": name_tags(&rds.security_group_name),
        }],
    })
}

pub fn instance_args(rds: &RdsContext) -> Json {
    json!({
        "DBInstanceIdentifier": rds.instance_identifier,
        "DBClusterIdentifier": rds.cluster_identifier,
        "DBInstanceClass": "db.serverless",
        "Engine": "aurora-postgresql",
        "Tags": name_tags(&rds.instance_identifier),
    })
}

fn scaling(rds: &RdsContext) -> Json {
    json!({"MinCapacity": rds.min_capacity, "MaxCapacity": rds.max_capacity})
}

/// cluster の作成の引数。`password` があれば static、無ければ AWS 管理のパスワード
pub fn create_cluster_args(
    rds: &RdsContext,
    security_group_id: &str,
    password: Option<&str>,
) -> Json {
    let mut args = json!({
        "DBClusterIdentifier": rds.cluster_identifier,
        "Engine": "aurora-postgresql",
        "EngineMode": "provisioned",
        "DatabaseName": rds.database_name,
        "MasterUsername": rds.master_username,
        "DBSubnetGroupName": rds.subnet_group_name,
        "VpcSecurityGroupIds": [security_group_id],
        "ServerlessV2ScalingConfiguration": scaling(rds),
        "BackupRetentionPeriod": rds.backup_retention_days,
        "Tags": name_tags(&rds.cluster_identifier),
    });
    match password {
        Some(password) => args["MasterUserPassword"] = json!(password),
        None => args["ManageMasterUserPassword"] = json!(true),
    }
    args
}

/// snapshot からの復元の引数 (パスワードは snapshot のものを引き継ぐ)
pub fn restore_cluster_args(rds: &RdsContext, security_group_id: &str, snapshot: &str) -> Json {
    json!({
        "DBClusterIdentifier": rds.cluster_identifier,
        "SnapshotIdentifier": snapshot,
        "Engine": "aurora-postgresql",
        "EngineMode": "provisioned",
        "DatabaseName": rds.database_name,
        "DBSubnetGroupName": rds.subnet_group_name,
        "VpcSecurityGroupIds": [security_group_id],
        "ServerlessV2ScalingConfiguration": scaling(rds),
        "BackupRetentionPeriod": rds.backup_retention_days,
        "Tags": name_tags(&rds.cluster_identifier),
    })
}

/// store に置く static の認証情報 (AWS 管理の secret と同じ username / password に、
/// 接続先を足した JSON)
pub fn static_credentials(
    rds: &RdsContext,
    password: &str,
    endpoint: Option<&str>,
    port: Option<i64>,
) -> String {
    pyjson::dumps(&json!({
        "username": rds.master_username,
        "password": password,
        "host": endpoint,
        "port": port,
        "dbname": rds.database_name,
        "engine": "postgres",
        "dbClusterIdentifier": rds.cluster_identifier,
    }))
}

fn modify_args(rds: &RdsContext) -> Json {
    json!({"DBClusterIdentifier": rds.cluster_identifier, "ApplyImmediately": true})
}

/// 完了待ち (Python 版はどれも 10 秒おき)
#[derive(Debug, Clone, Copy)]
pub struct RdsWaits {
    pub vpc: WaitOptions,
    /// 作成後の cluster / instance の available 待ち
    pub create: WaitOptions,
    /// modify の後の available 待ち
    pub modify: WaitOptions,
    pub delete: WaitOptions,
}

impl Default for RdsWaits {
    fn default() -> Self {
        Self {
            vpc: WaitOptions::default(),
            create: WaitOptions::new(1800, 10),
            modify: WaitOptions::new(600, 10),
            delete: WaitOptions::new(1800, 10),
        }
    }
}

pub struct RdsDeploy<'a, A, K, V> {
    pub aws: &'a A,
    pub stacks: &'a K,
    pub values: &'a V,
    pub waits: RdsWaits,
}

fn is_static(rds: &RdsContext) -> bool {
    rds.password_strategy == PasswordStrategy::Static
}

fn store_name(store: StoreType) -> &'static str {
    if store == StoreType::Ssm {
        "ssm"
    } else {
        "sm"
    }
}

fn vpc(rds: &RdsContext) -> Result<&VpcContext> {
    rds.vpc.as_ref().context("vpc context is not configured")
}

impl<A: RdsAws, K: Stacks, V: StoredValues> RdsDeploy<'_, A, K, V> {
    /// 何かを作る前の検査: 外部 VPC (manage = false) の stack が在ること
    pub async fn deploy_init(&self, rds: &RdsContext) -> Result<()> {
        if !rds.managed {
            return Ok(());
        }
        let vpc = vpc(rds)?;
        if vpc.manage {
            return Ok(());
        }
        let name = vpc_stack::stack_name(vpc);
        let description = self.stacks.describe(&vpc.region, &name).await?;
        if cfn_status(description.as_ref())? == ResourceStatus::NoExist {
            bail!("外部 VPC スタック '{name}' が見つかりません。");
        }
        Ok(())
    }

    /// VPC の stack の output から (private subnet の id, VPC の id)
    async fn vpc_network(&self, vpc: &VpcContext) -> Result<(Vec<String>, String)> {
        let name = vpc_stack::stack_name(vpc);
        let description = self.stacks.describe(&vpc.region, &name).await?;
        let outputs =
            stack_outputs(description.as_ref()).context("VPC stack output is not available")?;
        let export = vpc_stack::vpc_stack(vpc, None)?.export;
        let key = |name: &str| -> Result<String> {
            export[name]
                .as_str()
                .map(str::to_string)
                .with_context(|| format!("VPC の export 名 {name} がありません"))
        };
        let prefix = key("private_subnet_")?;
        let mut subnet_ids = Vec::new();
        for i in 1..20 {
            match outputs.get(&format!("{prefix}{i}")) {
                Some(id) => subnet_ids.push(id.clone()),
                None => break,
            }
        }
        if subnet_ids.is_empty() {
            bail!("No private subnets found in VPC stack");
        }
        let vpc_id = outputs
            .get(&key("vpc_id")?)
            .cloned()
            .context("VPC stack output に VPC の id がありません")?;
        Ok((subnet_ids, vpc_id))
    }

    async fn wait_cluster_available(&self, rds: &RdsContext, wait: WaitOptions) -> Result<()> {
        wait_until(
            wait,
            "Waiting for cluster to be available",
            &format!(
                "Cluster did not become available within {} seconds",
                wait.timeout_secs
            ),
            || async {
                let cluster = self.aws.cluster(&rds.cluster_identifier).await?;
                Ok(cluster.is_some_and(|c| c.status == "available"))
            },
        )
        .await
    }

    async fn wait_instance_available(&self, rds: &RdsContext, wait: WaitOptions) -> Result<()> {
        wait_until(
            wait,
            "Waiting for instance to be available",
            &format!(
                "Instance did not become available within {} seconds",
                wait.timeout_secs
            ),
            || async {
                let status = self.aws.instance_status(&rds.instance_identifier).await?;
                Ok(status.as_deref() == Some("available"))
            },
        )
        .await
    }

    /// cluster を作る (snapshot の指定があれば復元)。既に在れば何もしない。
    /// 戻り値は (今回作ったか, 生成した password)
    async fn create_or_restore_cluster(
        &self,
        rds: &RdsContext,
        security_group_id: &str,
        sink: &mut dyn Sink,
    ) -> Result<(bool, Option<String>)> {
        let identifier = &rds.cluster_identifier;
        if self.aws.cluster(identifier).await?.is_some() {
            sink.log(&format!(
                "Aurora cluster {identifier} already exists; skipping creation."
            ));
            return Ok((false, None));
        }
        if let Some(snapshot) = rds.snapshot_identifier.as_deref().filter(|s| !s.is_empty()) {
            sink.log(&format!(
                "Restoring Aurora cluster {identifier} from snapshot {snapshot}"
            ));
            let args = restore_cluster_args(rds, security_group_id, snapshot);
            self.aws.restore_cluster(&args).await?;
            return Ok((true, None));
        }
        sink.log(&format!("Creating Aurora cluster: {identifier}"));
        let password = if is_static(rds) {
            Some(master_password()?)
        } else {
            None
        };
        let args = create_cluster_args(rds, security_group_id, password.as_deref());
        self.aws.create_cluster(&args).await?;
        Ok((true, password))
    }

    pub async fn create(&self, rds: &RdsContext, sink: &mut dyn Sink) -> Result<()> {
        let vpc = vpc(rds)?;
        if vpc.manage {
            stacks::wait(
                self.stacks,
                &vpc.region,
                &vpc_stack::stack_name(vpc),
                "vpc",
                ResourceStatus::Completed,
                self.waits.vpc,
            )
            .await?;
        }
        let (subnet_ids, vpc_id) = self.vpc_network(vpc).await?;

        // 1. subnet group
        sink.log(&format!(
            "Creating DB Subnet Group: {}",
            rds.subnet_group_name
        ));
        let created = self
            .aws
            .create_subnet_group(&subnet_group_args(rds, &subnet_ids))
            .await?;
        if !created {
            sink.log(&format!(
                "DB Subnet Group {} already exists; reusing.",
                rds.subnet_group_name
            ));
        }

        // 2. security group
        let name = &rds.security_group_name;
        let security_group_id = match self.aws.security_group_id(name).await? {
            Some(id) => {
                sink.log(&format!("Security Group {name} already exists; reusing."));
                id
            }
            None => {
                sink.log(&format!("Creating Security Group: {name}"));
                self.aws
                    .create_security_group(&security_group_args(rds, &vpc_id))
                    .await?
            }
        };

        // 3. cluster。パスワードの設定し直しと static の保存は、今回作ったときだけ行う
        // (やり直しで static のパスワードを作り直さない。既存の cluster は update が合わせる)
        let (cluster_created, mut password) = self
            .create_or_restore_cluster(rds, &security_group_id, sink)
            .await?;

        // 4. instance
        let instance = &rds.instance_identifier;
        if self.aws.instance_status(instance).await?.is_some() {
            sink.log(&format!(
                "Aurora instance {instance} already exists; skipping creation."
            ));
        } else {
            sink.log(&format!("Creating Aurora instance: {instance}"));
            self.aws.create_instance(&instance_args(rds)).await?;
        }

        // 5. cluster が available でも instance が作成中のことがある。後の modify の前に
        // instance まで待つ
        sink.log("Waiting for Aurora cluster to become available...");
        self.wait_cluster_available(rds, self.waits.create).await?;
        sink.log("Waiting for Aurora instance to become available...");
        self.wait_instance_available(rds, self.waits.create).await?;

        // 6. snapshot から復元した cluster は元のパスワードを引き継ぐので、pocket が
        // 参照できる認証情報を設定し直す
        let restored = rds
            .snapshot_identifier
            .as_deref()
            .is_some_and(|s| !s.is_empty());
        if cluster_created && restored {
            let mut args = modify_args(rds);
            if is_static(rds) {
                sink.log("Setting a pocket-managed static master password...");
                let generated = master_password()?;
                args["MasterUserPassword"] = json!(generated);
                password = Some(generated);
            } else {
                sink.log(
                    "Switching master password to AWS-managed secret \
                     (ManageMasterUserPassword=True)...",
                );
                args["ManageMasterUserPassword"] = json!(true);
            }
            self.aws.modify_cluster(&args).await?;
            self.wait_cluster_available(rds, self.waits.modify).await?;
        }

        // 7. static: 生成したパスワードを pocket の store に置く (rotation はしない)
        if cluster_created && is_static(rds) {
            let password = password.context("password must be set for static credentials")?;
            self.store_static_credentials(rds, &password, sink).await?;
        }

        // 8. AWS 管理: 宣言された rotation の窓を当てる (作り直された secret は AWS の
        // 既定に戻っている)
        self.apply_rotation(rds, sink).await?;
        sink.success("Aurora cluster is now available.");
        Ok(())
    }

    async fn write_credentials(
        &self,
        rds: &RdsContext,
        store: StoreType,
        value: &str,
        sink: &mut dyn Sink,
    ) -> Result<()> {
        let name = &rds.credentials_secret_name;
        let result = self.values.put(name, store, value, &rds.region).await?;
        sink.success(&match (store, result) {
            (StoreType::Ssm, _) => format!("Stored static DB credentials in SSM parameter: {name}"),
            (_, PutResult::Created) => format!("Stored static DB credentials secret: {name}"),
            (_, PutResult::Updated) => format!("Updated static DB credentials secret: {name}"),
        });
        Ok(())
    }

    async fn store_static_credentials(
        &self,
        rds: &RdsContext,
        password: &str,
        sink: &mut dyn Sink,
    ) -> Result<()> {
        // 接続先は最新の cluster から取る
        let cluster = self.aws.cluster(&rds.cluster_identifier).await?;
        let value = static_credentials(
            rds,
            password,
            cluster.as_ref().and_then(|c| c.endpoint.as_deref()),
            cluster.as_ref().and_then(|c| c.port),
        );
        self.write_credentials(rds, rds.secret_store, &value, sink)
            .await
    }

    /// 接続情報の secret の ARN (rotation を当てる先。static + ssm には無い)
    async fn master_user_secret_arn(
        &self,
        rds: &RdsContext,
        cluster: Option<&ClusterState>,
    ) -> Result<Option<String>> {
        if is_static(rds) {
            if rds.secret_store != StoreType::Sm {
                return Ok(None);
            }
            return self
                .aws
                .static_secret_arn(&rds.credentials_secret_name)
                .await;
        }
        Ok(cluster.and_then(|c| c.master_user_secret_arn.clone()))
    }

    /// 宣言された rotation の窓を secret に当てる (違うときだけ)
    async fn apply_rotation(&self, rds: &RdsContext, sink: &mut dyn Sink) -> Result<()> {
        let Some(desired) = rds_status::desired_rotation(rds) else {
            return Ok(());
        };
        let cluster = self.aws.cluster(&rds.cluster_identifier).await?;
        let Some(arn) = self.master_user_secret_arn(rds, cluster.as_ref()).await? else {
            return Ok(());
        };
        let current = self.aws.rotation_rules(&arn).await?;
        if rds_status::rotation_matches(&desired, &current) {
            return Ok(());
        }
        let window: Vec<&str> = desired.values().filter_map(Json::as_str).collect();
        sink.log(&format!(
            "Setting master password rotation window (UTC): {}",
            window.join(" / ")
        ));
        self.aws.rotate_secret(&arn, &desired).await?;
        sink.success("Master password rotation window updated.");
        Ok(())
    }

    /// パスワードの管理方式と、static のときの置き場が望みどおりか
    async fn password_state_matches(
        &self,
        rds: &RdsContext,
        cluster: &ClusterState,
    ) -> Result<bool> {
        if !is_static(rds) {
            return Ok(cluster.has_master_user_secret);
        }
        if cluster.has_master_user_secret {
            return Ok(false);
        }
        let name = &rds.credentials_secret_name;
        if rds.secret_store == StoreType::Ssm {
            return self.aws.ssm_parameter_exists(name).await;
        }
        Ok(self.aws.static_secret_arn(name).await?.is_some())
    }

    /// 既にある cluster を望む設定に合わせる
    pub async fn update(&self, rds: &RdsContext, sink: &mut dyn Sink) -> Result<()> {
        let identifier = &rds.cluster_identifier;
        let cluster = self
            .aws
            .cluster(identifier)
            .await?
            .with_context(|| format!("Aurora cluster {identifier} が見つかりません。"))?;
        // 変更は 1 回の modify にまとめる (続けて撃つと、前の変更で modifying のまま
        // 弾かれる)。ApplyImmediately が無いと保持日数が次の maintenance window まで
        // 保留され、その間ずっと差分ありと判定される
        let mut args = modify_args(rds);
        let mut modified = false;
        if !rds_status::scaling_matches(rds, &cluster) {
            args["ServerlessV2ScalingConfiguration"] = scaling(rds);
            modified = true;
        }
        if !rds_status::backup_retention_matches(rds, &cluster) {
            args["BackupRetentionPeriod"] = json!(rds.backup_retention_days);
            modified = true;
        }
        if modified {
            sink.log("Updating RDS cluster configuration...");
            self.aws.modify_cluster(&args).await?;
            sink.success("RDS cluster configuration updated.");
        }
        if !self.password_state_matches(rds, &cluster).await? {
            if modified {
                self.wait_cluster_available(rds, self.waits.modify).await?;
            }
            if is_static(rds) {
                self.migrate_to_static(rds, &cluster, sink).await?;
            } else {
                self.migrate_to_managed(rds, sink).await?;
            }
        }
        // 移行で AWS 管理の secret が新しくできた場合も、ここで窓を当てる
        self.apply_rotation(rds, sink).await
    }

    async fn migrate_to_managed(&self, rds: &RdsContext, sink: &mut dyn Sink) -> Result<()> {
        sink.log("Migrating master password to AWS-managed...");
        let mut args = modify_args(rds);
        args["ManageMasterUserPassword"] = json!(true);
        self.aws.modify_cluster(&args).await?;
        self.wait_cluster_available(rds, self.waits.modify).await?;
        // pocket が持っていた認証情報は要らなくなるので、両方の store から消す
        for store in [StoreType::Sm, StoreType::Ssm] {
            self.values
                .delete(&rds.credentials_secret_name, store, &rds.region)
                .await?;
        }
        sink.success("Master password is now AWS-managed.");
        Ok(())
    }

    async fn migrate_to_static(
        &self,
        rds: &RdsContext,
        cluster: &ClusterState,
        sink: &mut dyn Sink,
    ) -> Result<()> {
        if cluster.has_master_user_secret {
            // AWS 管理を切って既知のパスワードを設定する (RDS が管理 secret を消す)
            sink.log("Migrating master password to pocket-managed static...");
            let password = master_password()?;
            let mut args = modify_args(rds);
            args["ManageMasterUserPassword"] = json!(false);
            args["MasterUserPassword"] = json!(password);
            self.aws.modify_cluster(&args).await?;
            self.wait_cluster_available(rds, self.waits.modify).await?;
            self.store_static_credentials(rds, &password, sink).await?;
            sink.success("Master password is now pocket-managed (static).");
            return Ok(());
        }
        // cluster は既に static。もう一方の store に認証情報があれば移す (パスワードは
        // 変えない)
        let name = &rds.credentials_secret_name;
        let other = if rds.secret_store == StoreType::Ssm {
            StoreType::Sm
        } else {
            StoreType::Ssm
        };
        if let Some(existing) = self.values.read(name, other, &rds.region).await? {
            sink.log(&format!(
                "Moving static DB credentials to {} store...",
                store_name(rds.secret_store)
            ));
            self.write_credentials(rds, rds.secret_store, &existing, sink)
                .await?;
            return self.values.delete(name, other, &rds.region).await;
        }
        // どの store にも無い: パスワードを設定し直して保存する
        sink.warning("Static credential not found in any store; resetting master password.");
        let password = master_password()?;
        let mut args = modify_args(rds);
        args["MasterUserPassword"] = json!(password);
        self.aws.modify_cluster(&args).await?;
        self.wait_cluster_available(rds, self.waits.modify).await?;
        self.store_static_credentials(rds, &password, sink).await
    }

    /// instance → cluster (最終 snapshot 付き) → security group → subnet group →
    /// static の認証情報、の順に消す
    pub async fn delete(&self, rds: &RdsContext, sink: &mut dyn Sink) -> Result<()> {
        let instance = &rds.instance_identifier;
        if let Some(status) = self.aws.instance_status(instance).await? {
            sink.log(&format!("Deleting Aurora instance: {instance}"));
            if status != "deleting" {
                self.aws.delete_instance(instance).await?;
            }
            let wait = self.waits.delete;
            wait_until(
                wait,
                "Waiting for instance deletion",
                &delete_timeout_message("Instance", wait),
                || async { Ok(self.aws.instance_status(instance).await?.is_none()) },
            )
            .await?;
            sink.success("Aurora instance deleted.");
        }

        let identifier = &rds.cluster_identifier;
        if let Some(cluster) = self.aws.cluster(identifier).await? {
            let seconds = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs();
            let snapshot = format!("{identifier}-final-{seconds}");
            sink.log(&format!("Deleting Aurora cluster: {identifier}"));
            if cluster.status != "deleting" {
                self.aws.delete_cluster(identifier, &snapshot).await?;
                sink.info(&format!("Final snapshot: {snapshot}"));
            }
            let wait = self.waits.delete;
            wait_until(
                wait,
                "Waiting for cluster deletion",
                &delete_timeout_message("Cluster", wait),
                || async { Ok(self.aws.cluster(identifier).await?.is_none()) },
            )
            .await?;
            sink.success("Aurora cluster deleted.");
        }

        let group = &rds.security_group_name;
        if let Some(id) = self.aws.security_group_id(group).await? {
            sink.log(&format!("Deleting Security Group: {group}"));
            self.aws.delete_security_group(&id).await?;
            sink.success("Security Group deleted.");
        }

        sink.log(&format!(
            "Deleting DB Subnet Group: {}",
            rds.subnet_group_name
        ));
        if self.aws.delete_subnet_group(&rds.subnet_group_name).await? {
            sink.success("DB Subnet Group deleted.");
        }

        if is_static(rds) {
            let name = &rds.credentials_secret_name;
            sink.log(&format!(
                "Deleting static DB credentials ({}): {name}",
                store_name(rds.secret_store)
            ));
            self.values
                .delete(name, rds.secret_store, &rds.region)
                .await?;
            sink.success("Static DB credentials deleted.");
        }
        Ok(())
    }
}

fn delete_timeout_message(what: &str, wait: WaitOptions) -> String {
    format!(
        "{what} の削除待機が {} 秒でタイムアウトしました。\
         AWS 側の削除は継続中の可能性があります。\
         同じ destroy コマンドを再実行すると待機を再開します。",
        wait.timeout_secs
    )
}

/// AWS に対する [`RdsAws`]
pub struct AwsRds<'a> {
    pub sdk: &'a SdkConfig,
    pub region: &'a str,
}

fn strings(value: &Json) -> Option<Vec<String>> {
    Some(
        value
            .as_array()?
            .iter()
            .filter_map(|item| item.as_str().map(str::to_string))
            .collect(),
    )
}

fn string(value: &Json) -> Option<String> {
    value.as_str().map(str::to_string)
}

fn int(value: &Json) -> Result<Option<i32>> {
    value
        .as_i64()
        .map(i32::try_from)
        .transpose()
        .map_err(Into::into)
}

fn rds_tags(value: &Json) -> Option<Vec<aws_sdk_rds::types::Tag>> {
    Some(
        value
            .as_array()?
            .iter()
            .map(|tag| {
                aws_sdk_rds::types::Tag::builder()
                    .set_key(string(&tag["Key"]))
                    .set_value(string(&tag["Value"]))
                    .build()
            })
            .collect(),
    )
}

fn scaling_configuration(
    value: &Json,
) -> Option<aws_sdk_rds::types::ServerlessV2ScalingConfiguration> {
    value.as_object().map(|config| {
        aws_sdk_rds::types::ServerlessV2ScalingConfiguration::builder()
            .set_min_capacity(config.get("MinCapacity").and_then(Json::as_f64))
            .set_max_capacity(config.get("MaxCapacity").and_then(Json::as_f64))
            .build()
    })
}

impl AwsRds<'_> {
    fn config(&self) -> SdkConfig {
        regional_config(self.sdk, self.region)
    }

    fn rds(&self) -> aws_sdk_rds::Client {
        aws_sdk_rds::Client::new(&self.config())
    }

    fn sm(&self) -> aws_sdk_secretsmanager::Client {
        aws_sdk_secretsmanager::Client::new(&self.config())
    }
}

impl RdsAws for AwsRds<'_> {
    async fn cluster(&self, identifier: &str) -> Result<Option<ClusterState>> {
        let response = self
            .rds()
            .describe_db_clusters()
            .db_cluster_identifier(identifier)
            .send()
            .await;
        match response {
            Ok(response) => Ok(response
                .db_clusters()
                .first()
                .map(rds_status::cluster_state)),
            Err(e) if has_code(&e, &["DBClusterNotFoundFault"]) => Ok(None),
            Err(e) => Err(sdk_error("rds describe_db_clusters", &e)),
        }
    }

    async fn instance_status(&self, identifier: &str) -> Result<Option<String>> {
        let response = self
            .rds()
            .describe_db_instances()
            .db_instance_identifier(identifier)
            .send()
            .await;
        match response {
            Ok(response) => Ok(response.db_instances().first().map(|instance| {
                instance
                    .db_instance_status()
                    .unwrap_or_default()
                    .to_string()
            })),
            Err(e) if has_code(&e, &["DBInstanceNotFound"]) => Ok(None),
            Err(e) => Err(sdk_error("rds describe_db_instances", &e)),
        }
    }

    async fn create_subnet_group(&self, args: &Json) -> Result<bool> {
        let created = self
            .rds()
            .create_db_subnet_group()
            .set_db_subnet_group_name(string(&args["DBSubnetGroupName"]))
            .set_db_subnet_group_description(string(&args["DBSubnetGroupDescription"]))
            .set_subnet_ids(strings(&args["SubnetIds"]))
            .set_tags(rds_tags(&args["Tags"]))
            .send()
            .await;
        match created {
            Ok(_) => Ok(true),
            // API が返す code は `DBSubnetGroupAlreadyExists` (model 上の名前は末尾に Fault)
            Err(e)
                if has_code(
                    &e,
                    &[
                        "DBSubnetGroupAlreadyExists",
                        "DBSubnetGroupAlreadyExistsFault",
                    ],
                ) =>
            {
                Ok(false)
            }
            Err(e) => Err(sdk_error("rds create_db_subnet_group", &e)),
        }
    }

    async fn delete_subnet_group(&self, name: &str) -> Result<bool> {
        let deleted = self
            .rds()
            .delete_db_subnet_group()
            .db_subnet_group_name(name)
            .send()
            .await;
        match deleted {
            Ok(_) => Ok(true),
            Err(e) if has_code(&e, &["DBSubnetGroupNotFoundFault"]) => Ok(false),
            Err(e) => Err(sdk_error("rds delete_db_subnet_group", &e)),
        }
    }

    async fn security_group_id(&self, name: &str) -> Result<Option<String>> {
        crate::ec2::security_group_id(&self.config(), name).await
    }

    async fn create_security_group(&self, args: &Json) -> Result<String> {
        let field = |key: &str| {
            args[key]
                .as_str()
                .ok_or_else(|| anyhow!("security group の引数 {key} がありません"))
        };
        crate::ec2::create_security_group(
            &self.config(),
            field("GroupName")?,
            field("Description")?,
            field("VpcId")?,
        )
        .await
    }

    async fn delete_security_group(&self, group_id: &str) -> Result<()> {
        crate::ec2::delete_security_group(&self.config(), group_id).await
    }

    async fn create_cluster(&self, args: &Json) -> Result<()> {
        self.rds()
            .create_db_cluster()
            .set_db_cluster_identifier(string(&args["DBClusterIdentifier"]))
            .set_engine(string(&args["Engine"]))
            .set_engine_mode(string(&args["EngineMode"]))
            .set_database_name(string(&args["DatabaseName"]))
            .set_master_username(string(&args["MasterUsername"]))
            .set_db_subnet_group_name(string(&args["DBSubnetGroupName"]))
            .set_vpc_security_group_ids(strings(&args["VpcSecurityGroupIds"]))
            .set_serverless_v2_scaling_configuration(scaling_configuration(
                &args["ServerlessV2ScalingConfiguration"],
            ))
            .set_backup_retention_period(int(&args["BackupRetentionPeriod"])?)
            .set_tags(rds_tags(&args["Tags"]))
            .set_master_user_password(string(&args["MasterUserPassword"]))
            .set_manage_master_user_password(args["ManageMasterUserPassword"].as_bool())
            .send()
            .await
            .map_err(|e| sdk_error("rds create_db_cluster", &e))?;
        Ok(())
    }

    async fn restore_cluster(&self, args: &Json) -> Result<()> {
        self.rds()
            .restore_db_cluster_from_snapshot()
            .set_db_cluster_identifier(string(&args["DBClusterIdentifier"]))
            .set_snapshot_identifier(string(&args["SnapshotIdentifier"]))
            .set_engine(string(&args["Engine"]))
            .set_engine_mode(string(&args["EngineMode"]))
            .set_database_name(string(&args["DatabaseName"]))
            .set_db_subnet_group_name(string(&args["DBSubnetGroupName"]))
            .set_vpc_security_group_ids(strings(&args["VpcSecurityGroupIds"]))
            .set_serverless_v2_scaling_configuration(scaling_configuration(
                &args["ServerlessV2ScalingConfiguration"],
            ))
            .set_backup_retention_period(int(&args["BackupRetentionPeriod"])?)
            .set_tags(rds_tags(&args["Tags"]))
            .send()
            .await
            .map_err(|e| sdk_error("rds restore_db_cluster_from_snapshot", &e))?;
        Ok(())
    }

    async fn create_instance(&self, args: &Json) -> Result<()> {
        self.rds()
            .create_db_instance()
            .set_db_instance_identifier(string(&args["DBInstanceIdentifier"]))
            .set_db_cluster_identifier(string(&args["DBClusterIdentifier"]))
            .set_db_instance_class(string(&args["DBInstanceClass"]))
            .set_engine(string(&args["Engine"]))
            .set_tags(rds_tags(&args["Tags"]))
            .send()
            .await
            .map_err(|e| sdk_error("rds create_db_instance", &e))?;
        Ok(())
    }

    async fn modify_cluster(&self, args: &Json) -> Result<()> {
        self.rds()
            .modify_db_cluster()
            .set_db_cluster_identifier(string(&args["DBClusterIdentifier"]))
            .set_apply_immediately(args["ApplyImmediately"].as_bool())
            .set_serverless_v2_scaling_configuration(scaling_configuration(
                &args["ServerlessV2ScalingConfiguration"],
            ))
            .set_backup_retention_period(int(&args["BackupRetentionPeriod"])?)
            .set_manage_master_user_password(args["ManageMasterUserPassword"].as_bool())
            .set_master_user_password(string(&args["MasterUserPassword"]))
            .send()
            .await
            .map_err(|e| sdk_error("rds modify_db_cluster", &e))?;
        Ok(())
    }

    async fn delete_instance(&self, identifier: &str) -> Result<()> {
        self.rds()
            .delete_db_instance()
            .db_instance_identifier(identifier)
            .skip_final_snapshot(true)
            .send()
            .await
            .map_err(|e| sdk_error("rds delete_db_instance", &e))?;
        Ok(())
    }

    async fn delete_cluster(&self, identifier: &str, final_snapshot: &str) -> Result<()> {
        self.rds()
            .delete_db_cluster()
            .db_cluster_identifier(identifier)
            .skip_final_snapshot(false)
            .final_db_snapshot_identifier(final_snapshot)
            .send()
            .await
            .map_err(|e| sdk_error("rds delete_db_cluster", &e))?;
        Ok(())
    }

    async fn static_secret_arn(&self, name: &str) -> Result<Option<String>> {
        match self.sm().describe_secret().secret_id(name).send().await {
            Ok(response) => Ok(response.arn().map(str::to_string)),
            Err(e) if has_code(&e, &["ResourceNotFoundException"]) => Ok(None),
            Err(e) => Err(sdk_error("secretsmanager describe_secret", &e)),
        }
    }

    async fn ssm_parameter_exists(&self, name: &str) -> Result<bool> {
        let client = aws_sdk_ssm::Client::new(&self.config());
        match client.get_parameter().name(name).send().await {
            Ok(_) => Ok(true),
            Err(e) if has_code(&e, &["ParameterNotFound"]) => Ok(false),
            Err(e) => Err(sdk_error("ssm get_parameter", &e)),
        }
    }

    async fn rotation_rules(&self, secret_arn: &str) -> Result<Map<String, Json>> {
        let response = self
            .sm()
            .describe_secret()
            .secret_id(secret_arn)
            .send()
            .await
            .map_err(|e| sdk_error("secretsmanager describe_secret", &e))?;
        let mut current = Map::new();
        if let Some(rules) = response.rotation_rules() {
            if let Some(expression) = rules.schedule_expression() {
                current.insert("ScheduleExpression".to_string(), Json::from(expression));
            }
            if let Some(duration) = rules.duration() {
                current.insert("Duration".to_string(), Json::from(duration));
            }
        }
        Ok(current)
    }

    async fn rotate_secret(&self, secret_arn: &str, rules: &Map<String, Json>) -> Result<()> {
        let rotation = aws_sdk_secretsmanager::types::RotationRulesType::builder()
            .set_schedule_expression(rules.get("ScheduleExpression").and_then(string))
            .set_duration(rules.get("Duration").and_then(string))
            .build();
        // RotateImmediately の既定は true で、付けないと deploy の瞬間に rotation が走る
        self.sm()
            .rotate_secret()
            .secret_id(secret_arn)
            .rotation_rules(rotation)
            .rotate_immediately(false)
            .send()
            .await
            .map_err(|e| sdk_error("secretsmanager rotate_secret", &e))?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;

    use super::*;
    use crate::deploy::poll::testing::{fast, run};
    use crate::deploy::stacks::fake::FakeStacks;
    use crate::deploy::stored_value::fake::FakeStoredValues;
    use crate::stack::testing::{context, read_json, repo_root, vector_context, vector_dirs};
    use crate::status::sink::{Recorded, Style};

    /// cluster / instance / security group / subnet group を持つ fake。
    /// 作成・変更の直後から available になる
    #[derive(Default)]
    struct FakeRds {
        cluster: RefCell<Option<ClusterState>>,
        instance: RefCell<Option<String>>,
        security_group: RefCell<Option<String>>,
        subnet_group: RefCell<bool>,
        static_secret: RefCell<Option<String>>,
        ssm_parameter: RefCell<bool>,
        rotation: RefCell<Map<String, Json>>,
        /// modify の後に cluster が available に戻らない
        stuck_after_modify: bool,
        calls: RefCell<Vec<String>>,
        args: RefCell<Vec<Json>>,
    }

    impl FakeRds {
        fn record(&self, call: &str, args: &Json) {
            self.calls.borrow_mut().push(call.to_string());
            self.args.borrow_mut().push(args.clone());
        }

        fn calls(&self) -> Vec<String> {
            self.calls.borrow().clone()
        }

        fn args_of(&self, call: &str) -> Vec<Json> {
            let calls = self.calls.borrow();
            let args = self.args.borrow();
            calls
                .iter()
                .zip(args.iter())
                .filter(|(name, _)| name.as_str() == call)
                .map(|(_, args)| args.clone())
                .collect()
        }

        /// 望む設定どおりの、available な cluster と instance を置く
        fn deployed(rds: &RdsContext) -> Self {
            let fake = Self::default();
            *fake.cluster.borrow_mut() = Some(ClusterState {
                status: "available".to_string(),
                min_capacity: Some(rds.min_capacity),
                max_capacity: Some(rds.max_capacity),
                backup_retention_period: Some(rds.backup_retention_days),
                has_master_user_secret: !is_static(rds),
                master_user_secret_arn: (!is_static(rds)).then(|| "arn:managed".to_string()),
                endpoint: Some("db.example".to_string()),
                port: Some(5432),
                arn: Some("arn:cluster".to_string()),
                engine_version: None,
            });
            *fake.instance.borrow_mut() = Some("available".to_string());
            *fake.security_group.borrow_mut() = Some("sg-1".to_string());
            *fake.subnet_group.borrow_mut() = true;
            fake
        }
    }

    impl RdsAws for FakeRds {
        async fn cluster(&self, _identifier: &str) -> Result<Option<ClusterState>> {
            Ok(self.cluster.borrow().clone())
        }

        async fn instance_status(&self, _identifier: &str) -> Result<Option<String>> {
            Ok(self.instance.borrow().clone())
        }

        async fn create_subnet_group(&self, args: &Json) -> Result<bool> {
            self.record("create_subnet_group", args);
            Ok(!self.subnet_group.replace(true))
        }

        async fn delete_subnet_group(&self, name: &str) -> Result<bool> {
            self.record("delete_subnet_group", &json!(name));
            Ok(self.subnet_group.replace(false))
        }

        async fn security_group_id(&self, _name: &str) -> Result<Option<String>> {
            Ok(self.security_group.borrow().clone())
        }

        async fn create_security_group(&self, args: &Json) -> Result<String> {
            self.record("create_security_group", args);
            *self.security_group.borrow_mut() = Some("sg-new".to_string());
            Ok("sg-new".to_string())
        }

        async fn delete_security_group(&self, group_id: &str) -> Result<()> {
            self.record("delete_security_group", &json!(group_id));
            *self.security_group.borrow_mut() = None;
            Ok(())
        }

        async fn create_cluster(&self, args: &Json) -> Result<()> {
            self.record("create_cluster", args);
            let managed = args["ManageMasterUserPassword"] == json!(true);
            *self.cluster.borrow_mut() = Some(ClusterState {
                status: "available".to_string(),
                min_capacity: args["ServerlessV2ScalingConfiguration"]["MinCapacity"].as_f64(),
                max_capacity: args["ServerlessV2ScalingConfiguration"]["MaxCapacity"].as_f64(),
                backup_retention_period: args["BackupRetentionPeriod"].as_i64(),
                has_master_user_secret: managed,
                master_user_secret_arn: managed.then(|| "arn:managed".to_string()),
                endpoint: Some("db.example".to_string()),
                port: Some(5432),
                arn: Some("arn:cluster".to_string()),
                engine_version: None,
            });
            Ok(())
        }

        async fn restore_cluster(&self, args: &Json) -> Result<()> {
            self.record("restore_cluster", args);
            *self.cluster.borrow_mut() = Some(ClusterState {
                status: "available".to_string(),
                endpoint: Some("db.example".to_string()),
                port: Some(5432),
                ..Default::default()
            });
            Ok(())
        }

        async fn create_instance(&self, args: &Json) -> Result<()> {
            self.record("create_instance", args);
            *self.instance.borrow_mut() = Some("available".to_string());
            Ok(())
        }

        async fn modify_cluster(&self, args: &Json) -> Result<()> {
            self.record("modify_cluster", args);
            let mut stored = self.cluster.borrow_mut();
            let cluster = stored.as_mut().expect("cluster");
            if let Some(managed) = args["ManageMasterUserPassword"].as_bool() {
                cluster.has_master_user_secret = managed;
                cluster.master_user_secret_arn = managed.then(|| "arn:managed".to_string());
            }
            if let Some(config) = args["ServerlessV2ScalingConfiguration"].as_object() {
                cluster.min_capacity = config["MinCapacity"].as_f64();
                cluster.max_capacity = config["MaxCapacity"].as_f64();
            }
            if let Some(days) = args["BackupRetentionPeriod"].as_i64() {
                cluster.backup_retention_period = Some(days);
            }
            if self.stuck_after_modify {
                cluster.status = "modifying".to_string();
            }
            Ok(())
        }

        async fn delete_instance(&self, identifier: &str) -> Result<()> {
            self.record("delete_instance", &json!(identifier));
            *self.instance.borrow_mut() = None;
            Ok(())
        }

        async fn delete_cluster(&self, identifier: &str, final_snapshot: &str) -> Result<()> {
            self.record("delete_cluster", &json!([identifier, final_snapshot]));
            *self.cluster.borrow_mut() = None;
            Ok(())
        }

        async fn static_secret_arn(&self, _name: &str) -> Result<Option<String>> {
            Ok(self.static_secret.borrow().clone())
        }

        async fn ssm_parameter_exists(&self, _name: &str) -> Result<bool> {
            Ok(*self.ssm_parameter.borrow())
        }

        async fn rotation_rules(&self, _secret_arn: &str) -> Result<Map<String, Json>> {
            Ok(self.rotation.borrow().clone())
        }

        async fn rotate_secret(&self, secret_arn: &str, rules: &Map<String, Json>) -> Result<()> {
            self.record("rotate_secret", &json!([secret_arn, rules]));
            *self.rotation.borrow_mut() = rules.clone();
            Ok(())
        }
    }

    fn waits() -> RdsWaits {
        RdsWaits {
            vpc: fast(3),
            create: fast(3),
            modify: fast(3),
            delete: fast(3),
        }
    }

    /// managed VPC + AWS 管理のパスワード + rotation の宣言
    fn managed() -> RdsContext {
        let ctx = vector_context("toml_vectors/db_backup.dev");
        let mut rds = ctx.rds.expect("rds");
        assert!(rds.vpc.as_ref().is_some_and(|vpc| vpc.manage));
        rds.password_strategy = PasswordStrategy::AwsManaged;
        rds.snapshot_identifier = None;
        rds.rotation_schedule = serde_json::from_value(json!("rate(30 days)")).expect("schedule");
        rds
    }

    fn static_rds(store: StoreType) -> RdsContext {
        let mut rds = managed();
        rds.password_strategy = PasswordStrategy::Static;
        rds.rotation_schedule = None;
        rds.rotation_duration = None;
        rds.secret_store = store;
        rds
    }

    /// VPC の stack (output つき) が完了している状態
    fn vpc_stacks(rds: &RdsContext) -> FakeStacks {
        let vpc = rds.vpc.as_ref().expect("vpc");
        let stack = vpc_stack::vpc_stack(vpc, None).expect("vpc stack");
        let export = |name: &str| stack.export[name].as_str().expect("export").to_string();
        let subnet = export("private_subnet_");
        let outputs = [
            (export("vpc_id"), "vpc-1".to_string()),
            (format!("{subnet}1"), "subnet-a".to_string()),
            (format!("{subnet}2"), "subnet-b".to_string()),
        ];
        let pairs: Vec<(&str, &str)> = outputs
            .iter()
            .map(|(key, value)| (key.as_str(), value.as_str()))
            .collect();
        let stacks = FakeStacks::default();
        stacks.put_synced(&stack, &pairs);
        stacks
    }

    struct World {
        aws: FakeRds,
        stacks: FakeStacks,
        values: FakeStoredValues,
    }

    impl World {
        fn new(rds: &RdsContext, aws: FakeRds) -> Self {
            Self {
                aws,
                stacks: vpc_stacks(rds),
                values: FakeStoredValues::default(),
            }
        }

        fn deploy(&self) -> RdsDeploy<'_, FakeRds, FakeStacks, FakeStoredValues> {
            RdsDeploy {
                aws: &self.aws,
                stacks: &self.stacks,
                values: &self.values,
                waits: waits(),
            }
        }
    }

    #[test]
    fn test_api_arguments_match_python() {
        let root = repo_root();
        let mut count = 0;
        for dir in vector_dirs() {
            let path = dir.join("documents.json");
            if !path.is_file() {
                continue;
            }
            let documents = read_json(&path);
            let want = &documents["rds"];
            if want.is_null() {
                continue;
            }
            let ctx = context(&root, &read_json(&dir.join("manifest.json")));
            let rds = ctx.rds.as_ref().expect("rds");
            let label = dir.display().to_string();
            let subnets: Vec<String> = strings(&want["subnet_ids"]).expect("subnets");
            let group = want["security_group_id"].as_str().expect("sg");
            let password = want["master_password"].as_str().expect("password");
            assert_eq!(
                subnet_group_args(rds, &subnets),
                want["create_db_subnet_group"],
                "{label}"
            );
            assert_eq!(
                security_group_args(rds, want["vpc_id"].as_str().expect("vpc")),
                want["create_security_group"],
                "{label}"
            );
            assert_eq!(instance_args(rds), want["create_db_instance"], "{label}");
            assert_eq!(
                create_cluster_args(rds, group, Some(password)),
                want["create_cluster_static"],
                "{label}"
            );
            assert_eq!(
                create_cluster_args(rds, group, None),
                want["create_cluster_managed"],
                "{label}"
            );
            assert_eq!(
                restore_cluster_args(rds, group, want["snapshot_identifier"].as_str().unwrap()),
                want["restore_cluster"],
                "{label}"
            );
            let rotation = rds_status::desired_rotation(rds).map_or(Json::Null, Json::Object);
            assert_eq!(rotation, want["rotation_rules"], "{label}");
            assert_eq!(
                static_credentials(
                    rds,
                    password,
                    want["endpoint"].as_str(),
                    want["port"].as_i64()
                ),
                want["static_credentials"].as_str().expect("credentials"),
                "{label}"
            );
            let restores = rds
                .snapshot_identifier
                .as_deref()
                .is_some_and(|s| !s.is_empty());
            let api = if restores {
                "restore_db_cluster_from_snapshot"
            } else {
                "create_db_cluster"
            };
            assert_eq!(want["cluster_api"], api, "{label}");
            count += 1;
        }
        assert!(count >= 6, "vector が少なすぎます: {count}");
    }

    #[test]
    fn test_create_runs_every_step_in_order() {
        let rds = managed();
        let world = World::new(&rds, FakeRds::default());
        let mut sink = Recorded::default();
        run(world.deploy().create(&rds, &mut sink)).expect("create");
        assert_eq!(
            world.aws.calls(),
            [
                "create_subnet_group",
                "create_security_group",
                "create_cluster",
                "create_instance",
                "rotate_secret",
            ]
        );
        let subnet = &world.aws.args_of("create_subnet_group")[0];
        assert_eq!(subnet["SubnetIds"], json!(["subnet-a", "subnet-b"]));
        assert_eq!(
            world.aws.args_of("create_security_group")[0]["VpcId"],
            "vpc-1"
        );
        let cluster = &world.aws.args_of("create_cluster")[0];
        assert_eq!(cluster["VpcSecurityGroupIds"], json!(["sg-new"]));
        assert_eq!(cluster["ManageMasterUserPassword"], true);
        // AWS 管理なので pocket の store には何も置かない
        assert!(world.values.calls().is_empty());
        let rotate = &world.aws.args_of("rotate_secret")[0];
        assert_eq!(rotate[0], "arn:managed");
        assert_eq!(
            sink.0.last().map(|r| (r.1, r.2.as_str())),
            Some((Style::Success, "Aurora cluster is now available."))
        );
    }

    #[test]
    fn test_create_reuses_existing_resources() {
        let rds = managed();
        let aws = FakeRds::deployed(&rds);
        *aws.rotation.borrow_mut() = rds_status::desired_rotation(&rds).expect("rotation");
        let world = World::new(&rds, aws);
        let mut sink = Recorded::default();
        run(world.deploy().create(&rds, &mut sink)).expect("create");
        // subnet group は作成を試みて「既にある」を受ける。他は作らない
        assert_eq!(world.aws.calls(), ["create_subnet_group"]);
        let logs: Vec<&str> = sink.0.iter().map(|r| r.2.as_str()).collect();
        assert!(logs.iter().any(|l| l.ends_with("already exists; reusing.")));
        assert!(logs
            .iter()
            .any(|l| l.ends_with("already exists; skipping creation.")));
    }

    #[test]
    fn test_create_static_stores_generated_password() {
        for store in [StoreType::Sm, StoreType::Ssm] {
            let rds = static_rds(store);
            let world = World::new(&rds, FakeRds::default());
            let mut sink = Recorded::default();
            run(world.deploy().create(&rds, &mut sink)).expect("create");
            let password = world.aws.args_of("create_cluster")[0]["MasterUserPassword"]
                .as_str()
                .expect("password")
                .to_string();
            assert_eq!(password.len(), 32);
            let stored = world
                .values
                .get(store, &rds.credentials_secret_name)
                .expect("stored");
            let stored: Json = serde_json::from_str(&stored).expect("JSON");
            assert_eq!(stored["password"], password.as_str());
            assert_eq!(stored["host"], "db.example");
            assert_eq!(stored["port"], 5432);
            assert!(!world.aws.calls().contains(&"rotate_secret".to_string()));
        }
    }

    #[test]
    fn test_create_does_not_regenerate_static_password_on_rerun() {
        // cluster が既にある (前回の deploy が途中で止まった) ときは保存し直さない
        let rds = static_rds(StoreType::Sm);
        let world = World::new(&rds, FakeRds::deployed(&rds));
        let mut sink = Recorded::default();
        run(world.deploy().create(&rds, &mut sink)).expect("create");
        assert!(world.values.calls().is_empty());
        assert!(!world.aws.calls().contains(&"modify_cluster".to_string()));
    }

    #[test]
    fn test_create_from_snapshot_resets_password_only_when_restored_now() {
        let mut rds = managed();
        rds.snapshot_identifier = Some("snap-1".to_string());
        let world = World::new(&rds, FakeRds::default());
        let mut sink = Recorded::default();
        run(world.deploy().create(&rds, &mut sink)).expect("create");
        assert_eq!(
            world.aws.calls(),
            [
                "create_subnet_group",
                "create_security_group",
                "restore_cluster",
                "create_instance",
                "modify_cluster",
                "rotate_secret",
            ]
        );
        let modify = &world.aws.args_of("modify_cluster")[0];
        assert_eq!(modify["ManageMasterUserPassword"], true);
        assert_eq!(modify["ApplyImmediately"], true);

        // static: 復元の後に生成したパスワードを設定し、同じ値を保存する
        let mut rds = static_rds(StoreType::Sm);
        rds.snapshot_identifier = Some("snap-1".to_string());
        let world = World::new(&rds, FakeRds::default());
        run(world.deploy().create(&rds, &mut sink)).expect("create");
        let password = world.aws.args_of("modify_cluster")[0]["MasterUserPassword"].clone();
        let stored = world
            .values
            .get(StoreType::Sm, &rds.credentials_secret_name)
            .expect("stored");
        let stored: Json = serde_json::from_str(&stored).expect("JSON");
        assert_eq!(stored["password"], password);

        // やり直し (cluster が既にある) では設定し直さない
        let world = World::new(&rds, FakeRds::deployed(&rds));
        run(world.deploy().create(&rds, &mut sink)).expect("create");
        assert!(!world.aws.calls().contains(&"modify_cluster".to_string()));
    }

    #[test]
    fn test_create_requires_vpc_outputs_and_available_cluster() {
        let rds = managed();
        let mut world = World::new(&rds, FakeRds::default());
        // output の無い VPC stack
        let stack = vpc_stack::vpc_stack(rds.vpc.as_ref().unwrap(), None).unwrap();
        world.stacks = FakeStacks::default();
        world.stacks.put_synced(&stack, &[]);
        let mut sink = Recorded::default();
        let error = run(world.deploy().create(&rds, &mut sink)).expect_err("output 無し");
        assert_eq!(error.to_string(), "VPC stack output is not available");
        assert!(world.aws.calls().is_empty());
    }

    #[test]
    fn test_update_merges_changes_into_one_modify() {
        let rds = managed();
        let aws = FakeRds::deployed(&rds);
        *aws.rotation.borrow_mut() = rds_status::desired_rotation(&rds).expect("rotation");
        {
            let mut cluster = aws.cluster.borrow_mut();
            let cluster = cluster.as_mut().unwrap();
            cluster.max_capacity = Some(rds.max_capacity + 1.0);
            cluster.backup_retention_period = Some(rds.backup_retention_days + 1);
        }
        let world = World::new(&rds, aws);
        let mut sink = Recorded::default();
        run(world.deploy().update(&rds, &mut sink)).expect("update");
        assert_eq!(world.aws.calls(), ["modify_cluster"]);
        let args = &world.aws.args_of("modify_cluster")[0];
        assert_eq!(
            args,
            &json!({
                "DBClusterIdentifier": rds.cluster_identifier,
                "ApplyImmediately": true,
                "ServerlessV2ScalingConfiguration":
                    {"MinCapacity": rds.min_capacity, "MaxCapacity": rds.max_capacity},
                "BackupRetentionPeriod": rds.backup_retention_days,
            })
        );
        // 2 回目は何もしない
        run(world.deploy().update(&rds, &mut sink)).expect("update");
        assert_eq!(world.aws.calls().len(), 1);
    }

    #[test]
    fn test_update_applies_rotation_only_when_it_differs() {
        let rds = managed();
        let world = World::new(&rds, FakeRds::deployed(&rds));
        let mut sink = Recorded::default();
        run(world.deploy().update(&rds, &mut sink)).expect("update");
        assert_eq!(world.aws.calls(), ["rotate_secret"]);
        let args = &world.aws.args_of("rotate_secret")[0];
        assert_eq!(args[1]["ScheduleExpression"], "rate(30 days)");
        run(world.deploy().update(&rds, &mut sink)).expect("update");
        assert_eq!(world.aws.calls().len(), 1);
    }

    #[test]
    fn test_update_migrates_static_to_managed() {
        let rds = managed();
        let aws = FakeRds::deployed(&static_rds(StoreType::Sm));
        let world = World::new(&rds, aws);
        world
            .values
            .set(StoreType::Sm, &rds.credentials_secret_name, "{}");
        let mut sink = Recorded::default();
        run(world.deploy().update(&rds, &mut sink)).expect("update");
        assert_eq!(world.aws.calls(), ["modify_cluster", "rotate_secret"]);
        assert_eq!(
            world.aws.args_of("modify_cluster")[0]["ManageMasterUserPassword"],
            true
        );
        // pocket の認証情報は両方の store から消す
        let name = &rds.credentials_secret_name;
        assert_eq!(
            world.values.calls(),
            [format!("delete sm {name}"), format!("delete ssm {name}")]
        );
    }

    #[test]
    fn test_update_migrates_managed_to_static() {
        let rds = static_rds(StoreType::Ssm);
        let world = World::new(&rds, FakeRds::deployed(&managed()));
        let mut sink = Recorded::default();
        run(world.deploy().update(&rds, &mut sink)).expect("update");
        let args = &world.aws.args_of("modify_cluster")[0];
        assert_eq!(args["ManageMasterUserPassword"], false);
        let stored = world
            .values
            .get(StoreType::Ssm, &rds.credentials_secret_name)
            .expect("stored");
        let stored: Json = serde_json::from_str(&stored).expect("JSON");
        assert_eq!(stored["password"], args["MasterUserPassword"]);
    }

    #[test]
    fn test_update_moves_static_credentials_between_stores() {
        // cluster は static。認証情報は SM にあり、望む置き場は SSM
        let rds = static_rds(StoreType::Ssm);
        let world = World::new(&rds, FakeRds::deployed(&rds));
        let name = rds.credentials_secret_name.clone();
        world
            .values
            .set(StoreType::Sm, &name, "{\"password\": \"keep\"}");
        let mut sink = Recorded::default();
        run(world.deploy().update(&rds, &mut sink)).expect("update");
        assert!(world.aws.calls().is_empty(), "パスワードは変えない");
        assert_eq!(
            world.values.get(StoreType::Ssm, &name).as_deref(),
            Some("{\"password\": \"keep\"}")
        );
        assert_eq!(world.values.get(StoreType::Sm, &name), None);
    }

    #[test]
    fn test_update_resets_password_when_no_store_has_credentials() {
        let rds = static_rds(StoreType::Sm);
        let world = World::new(&rds, FakeRds::deployed(&rds));
        let mut sink = Recorded::default();
        run(world.deploy().update(&rds, &mut sink)).expect("update");
        let args = &world.aws.args_of("modify_cluster")[0];
        assert!(args["ManageMasterUserPassword"].is_null());
        assert!(args["MasterUserPassword"].is_string());
        assert!(world
            .values
            .get(StoreType::Sm, &rds.credentials_secret_name)
            .is_some());
        assert!(sink.0.iter().any(|r| r.1 == Style::Warning));
    }

    #[test]
    fn test_update_stops_when_cluster_does_not_return_to_available() {
        let rds = managed();
        let mut aws = FakeRds::deployed(&static_rds(StoreType::Sm));
        aws.stuck_after_modify = true;
        let world = World::new(&rds, aws);
        let mut sink = Recorded::default();
        let error = run(world.deploy().update(&rds, &mut sink)).expect_err("timeout");
        assert!(error
            .to_string()
            .starts_with("Cluster did not become available"));
        // 待機が尽きたら、認証情報の削除にも rotation にも進まない
        assert!(world.values.calls().is_empty());
        assert_eq!(world.aws.calls(), ["modify_cluster"]);
    }

    #[test]
    fn test_delete_order_and_skips_delete_api_while_deleting() {
        let rds = static_rds(StoreType::Sm);
        let world = World::new(&rds, FakeRds::deployed(&rds));
        world
            .values
            .set(StoreType::Sm, &rds.credentials_secret_name, "{}");
        let mut sink = Recorded::default();
        run(world.deploy().delete(&rds, &mut sink)).expect("delete");
        assert_eq!(
            world.aws.calls(),
            [
                "delete_instance",
                "delete_cluster",
                "delete_security_group",
                "delete_subnet_group",
            ]
        );
        let snapshot = world.aws.args_of("delete_cluster")[0][1]
            .as_str()
            .expect("snapshot")
            .to_string();
        assert!(snapshot.starts_with(&format!("{}-final-", rds.cluster_identifier)));
        assert_eq!(
            world.values.calls(),
            [format!("delete sm {}", rds.credentials_secret_name)]
        );

        // 何も無い状態では、subnet group の削除 (無ければ無視) と認証情報の削除だけ
        let mut sink = Recorded::default();
        run(world.deploy().delete(&rds, &mut sink)).expect("delete");
        assert_eq!(world.aws.calls().len(), 5);
        assert!(!sink.0.iter().any(|r| r.2 == "DB Subnet Group deleted."));

        // 削除中の instance には削除の API を呼ばず、消えるのを待つ
        let world = World::new(&rds, FakeRds::default());
        *world.aws.instance.borrow_mut() = Some("deleting".to_string());
        let error = run(world.deploy().delete(&rds, &mut sink)).expect_err("timeout");
        assert!(error.to_string().starts_with("Instance の削除待機が"));
        assert!(world.aws.calls().is_empty());
    }

    #[test]
    fn test_deploy_init_requires_external_vpc_stack() {
        let ctx = vector_context("toml/rds_external_vpc.dev");
        let rds = ctx.rds.expect("rds");
        assert!(!rds.vpc.as_ref().unwrap().manage);
        let world = World::new(&rds, FakeRds::default());
        run(world.deploy().deploy_init(&rds)).expect("stack あり");
        let mut world = world;
        world.stacks = FakeStacks::default();
        let error = run(world.deploy().deploy_init(&rds)).expect_err("stack 無し");
        assert!(error.to_string().starts_with("外部 VPC スタック '"));
        // managed VPC はまだ作られていなくてよい
        let rds = managed();
        run(world.deploy().deploy_init(&rds)).expect("managed");
    }

    #[test]
    fn test_default_waits_match_python() {
        let waits = RdsWaits::default();
        for (wait, attempts) in [(waits.create, 180), (waits.modify, 60), (waits.delete, 180)] {
            assert_eq!((wait.attempts, wait.interval.as_secs()), (attempts, 10));
        }
    }
}
