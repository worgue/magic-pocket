from __future__ import annotations

import json
import secrets
import string
import time
from functools import cached_property
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from pocket import secret_store
from pocket.resources.base import ResourceStatus
from pocket.utils import echo
from pocket_cli.resources.aws.cloudformation import VpcStack
from pocket_cli.resources.aws.poll import wait_until
from pocket_cli.resources.vpc import Vpc

if TYPE_CHECKING:
    from pocket.context import RdsContext


class RdsResourceIsNotReady(Exception):
    pass


def _generate_master_password(length: int = 32) -> str:
    """Aurora PostgreSQL マスターパスワードとして安全なランダム文字列を生成する。

    マスターパスワードは ``/`` ``"`` ``@`` スペースを使えない。
    さらにクォート事故を避けるため ``'`` ``\\`` `` ` `` も除外し、
    URL 安全な英数字 + 限定記号から ``secrets`` で選ぶ。
    """
    alphabet = string.ascii_letters + string.digits + "-_.!#%+="
    return "".join(secrets.choice(alphabet) for _ in range(length))


class Rds:
    context: RdsContext

    def __init__(self, context: RdsContext) -> None:
        self.context = context
        self._rds_client = boto3.client("rds", region_name=context.region)
        self._ec2_client = boto3.client("ec2", region_name=context.region)
        self._sm_client = boto3.client("secretsmanager", region_name=context.region)
        self._ssm_client = boto3.client("ssm", region_name=context.region)

    def _describe_cluster(self) -> dict | None:
        """クラスタを都度 API で引く (キャッシュしない)。create() の存在判定など、
        作成前後で最新値が要る箇所はこちらを使う。"""
        try:
            res = self._rds_client.describe_db_clusters(
                DBClusterIdentifier=self.context.cluster_identifier
            )
            clusters = res["DBClusters"]
            if clusters:
                return clusters[0]
            return None
        except ClientError as e:
            if e.response["Error"]["Code"] == "DBClusterNotFoundFault":
                return None
            raise

    @cached_property
    def cluster(self) -> dict | None:
        return self._describe_cluster()

    def _describe_instance(self) -> dict | None:
        try:
            res = self._rds_client.describe_db_instances(
                DBInstanceIdentifier=self.context.instance_identifier
            )
            instances = res["DBInstances"]
            if instances:
                return instances[0]
            return None
        except ClientError as e:
            if e.response["Error"]["Code"] == "DBInstanceNotFound":
                return None
            raise

    @cached_property
    def instance(self) -> dict | None:
        return self._describe_instance()

    def _describe_security_group(self) -> dict | None:
        res = self._ec2_client.describe_security_groups(
            Filters=[
                {"Name": "tag:Name", "Values": [self.context.security_group_name]},
            ]
        )
        groups = res["SecurityGroups"]
        if groups:
            return groups[0]
        return None

    @cached_property
    def _security_group(self) -> dict | None:
        return self._describe_security_group()

    @property
    def security_group_id(self) -> str | None:
        if self._security_group:
            return self._security_group["GroupId"]
        return None

    @cached_property
    def _static_secret(self) -> dict | None:
        """password_strategy = "static" で pocket が作成した認証情報 secret。"""
        try:
            return self._sm_client.describe_secret(
                SecretId=self.context.credentials_secret_name
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                return None
            raise

    @property
    def master_user_secret_arn(self) -> str | None:
        if self.context.password_strategy == "static":  # noqa: S105 戦略名/保存先種別であって secret 値ではない
            # secret_store=ssm の場合は Secrets Manager に secret を作らない。
            if self.context.secret_store != "sm":  # noqa: S105 戦略名/保存先種別であって secret 値ではない
                return None
            return self._static_secret["ARN"] if self._static_secret else None
        if self.cluster and "MasterUserSecret" in self.cluster:
            return self.cluster["MasterUserSecret"]["SecretArn"]
        return None

    @property
    def static_ssm_param_name(self) -> str | None:
        """static + secret_store=ssm のとき、認証情報を保存する SSM パラメータ名。"""
        if self.context.password_strategy == "static" and (  # noqa: S105 戦略名/保存先種別であって secret 値ではない
            self.context.secret_store == "ssm"  # noqa: S105 戦略名/保存先種別であって secret 値ではない
        ):
            return self.context.credentials_secret_name
        return None

    @property
    def master_user_secret_kms_key_id(self) -> str | None:
        # static は既定の aws/secretsmanager キーで暗号化するため、
        # secretsmanager:GetSecretValue のみで復号でき KMS の明示付与は不要。
        if self.context.password_strategy == "static":  # noqa: S105 戦略名/保存先種別であって secret 値ではない
            return None
        if self.cluster and "MasterUserSecret" in self.cluster:
            return self.cluster["MasterUserSecret"].get("KmsKeyId")
        return None

    @property
    def endpoint(self) -> str | None:
        if self.cluster:
            return self.cluster.get("Endpoint")
        return None

    @property
    def port(self) -> int | None:
        if self.cluster:
            return self.cluster.get("Port")
        return None

    @property
    def database_name(self) -> str:
        return self.context.database_name

    @property
    def status(self) -> ResourceStatus:
        if not self.context.managed:
            return "COMPLETED"
        if self.cluster is None:
            return "NOEXIST"
        cluster_status = self.cluster["Status"]
        if cluster_status == "available":
            if not self._scaling_config_matches():
                return "REQUIRE_UPDATE"
            if not self._password_state_matches():
                return "REQUIRE_UPDATE"
            return "COMPLETED"
        if cluster_status in (
            "failed",
            "inaccessible-encryption-credentials",
            "storage-full",
            "stopped",
        ):
            return "FAILED"
        # creating / modifying / deleting のほか backing-up / upgrading /
        # maintenance / starting / resetting-master-credentials 等の一時 status。
        # FAILED にするとバックアップウィンドウ中の deploy が誤って中断する
        return "PROGRESS"

    @property
    def description(self):
        if not self.context.managed:
            return "Reference existing RDS (secret_arn: %s)" % self.context.secret_arn
        return (
            "Create Aurora PostgreSQL Serverless v2 cluster: %s"
            % self.context.cluster_identifier
        )

    def _scaling_config_matches(self) -> bool:
        if not self.cluster:
            return False
        config = self.cluster.get("ServerlessV2ScalingConfiguration", {})
        return (
            config.get("MinCapacity") == self.context.min_capacity
            and config.get("MaxCapacity") == self.context.max_capacity
        )

    def _actual_is_managed(self) -> bool:
        """クラスタが現状 ManageMasterUserPassword 管理かどうか。"""
        return bool(self.cluster and "MasterUserSecret" in self.cluster)

    def _store_has_credential(self, store: str) -> bool:
        name = self.context.credentials_secret_name
        if store == "ssm":
            try:
                self._ssm_client.get_parameter(Name=name)
                return True
            except ClientError as e:
                if e.response["Error"]["Code"] == "ParameterNotFound":
                    return False
                raise
        return self._static_secret is not None

    def _password_state_matches(self) -> bool:
        """クラスタの現状が望む password_strategy / secret_store と一致するか。

        不一致なら status が REQUIRE_UPDATE を返し、update() が移行する。
        """
        if self.context.password_strategy == "aws-managed":  # noqa: S105 戦略名/保存先種別であって secret 値ではない
            return self._actual_is_managed()
        # 望む = static
        if self._actual_is_managed():
            return False  # aws-managed → static の移行が必要
        # クラスタは static。望む store に認証情報が在るか
        return self._store_has_credential(self.context.secret_store)

    def _get_vpc_stack(self) -> VpcStack:
        if not self.context.vpc:
            raise RuntimeError("vpc context is not configured")
        return VpcStack(self.context.vpc)

    def _get_vpc_subnet_ids(self) -> list[str]:
        vpc_stack = self._get_vpc_stack()
        output = vpc_stack.output
        export = vpc_stack.export
        if not output:
            raise RuntimeError("VPC stack output is not available")
        prefix = export["private_subnet_"]
        subnet_ids = []
        for i in range(1, 20):
            key = f"{prefix}{i}"
            if key in output:
                subnet_ids.append(output[key])
            else:
                break
        if not subnet_ids:
            raise RuntimeError("No private subnets found in VPC stack")
        return subnet_ids

    def _get_vpc_id(self) -> str:
        vpc_stack = self._get_vpc_stack()
        output = vpc_stack.output
        export = vpc_stack.export
        if not output:
            raise RuntimeError("VPC stack output is not available")
        return output[export["vpc_id"]]

    def state_info(self):
        if not self.context.managed:
            return {
                "rds": {
                    "managed": False,
                    "secret_arn": self.context.secret_arn,
                    "security_group_id": self.context.security_group_id,
                }
            }
        return {
            "rds": {
                "managed": True,
                "cluster_identifier": self.context.cluster_identifier,
                "security_group_id": self.security_group_id,
            }
        }

    def deploy_init(self):
        if not self.context.managed:
            return
        if not self.context.vpc:
            raise RuntimeError("vpc context is not configured")
        vpc_stack = Vpc(self.context.vpc).stack
        if not self.context.vpc.manage:
            if vpc_stack.status == "NOEXIST":
                raise ValueError(
                    f"外部 VPC スタック '{vpc_stack.name}' が見つかりません。"
                )
        # managed VPC の COMPLETED 待ちは deploy_resources で行う
        # （deploy_init 時点ではまだ VPC が作成されていない場合がある）

    def _ensure_subnet_group(self, subnet_ids: list[str]) -> None:
        """DB Subnet Group を作成 (既存なら再利用)。"""
        try:
            echo.log("Creating DB Subnet Group: %s" % self.context.subnet_group_name)
            self._rds_client.create_db_subnet_group(
                DBSubnetGroupName=self.context.subnet_group_name,
                DBSubnetGroupDescription="Aurora subnet group for %s"
                % self.context.cluster_identifier,
                SubnetIds=subnet_ids,
                Tags=[{"Key": "Name", "Value": self.context.subnet_group_name}],
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "DBSubnetGroupAlreadyExistsFault":
                raise
            echo.log(
                "DB Subnet Group %s already exists; reusing."
                % self.context.subnet_group_name
            )

    def _ensure_security_group(self, vpc_id: str) -> str:
        """RDS 用 Security Group を作成 (Name タグで既存を検出したら再利用)。"""
        existing_sg = self._describe_security_group()
        if existing_sg:
            echo.log(
                "Security Group %s already exists; reusing."
                % self.context.security_group_name
            )
            return existing_sg["GroupId"]
        echo.log("Creating Security Group: %s" % self.context.security_group_name)
        sg_res = self._ec2_client.create_security_group(
            GroupName=self.context.security_group_name,
            Description="RDS Aurora security group for %s"
            % self.context.cluster_identifier,
            VpcId=vpc_id,
            TagSpecifications=[
                {
                    "ResourceType": "security-group",
                    "Tags": [
                        {"Key": "Name", "Value": self.context.security_group_name}
                    ],
                }
            ],
        )
        return sg_res["GroupId"]

    def _create_or_restore_cluster(
        self, sg_id: str, static: bool
    ) -> tuple[bool, str | None]:
        """クラスタを作成/復元 (既存なら skip)。

        戻り値は (このセッションで新規作成/復元したか, 生成した master password)。
        """
        if self._describe_cluster() is not None:
            echo.log(
                "Aurora cluster %s already exists; skipping creation."
                % self.context.cluster_identifier
            )
            return False, None
        if self.context.snapshot_identifier:
            echo.log(
                "Restoring Aurora cluster %s from snapshot %s"
                % (self.context.cluster_identifier, self.context.snapshot_identifier)
            )
            self._rds_client.restore_db_cluster_from_snapshot(
                DBClusterIdentifier=self.context.cluster_identifier,
                SnapshotIdentifier=self.context.snapshot_identifier,
                Engine="aurora-postgresql",
                EngineMode="provisioned",
                DatabaseName=self.context.database_name,
                DBSubnetGroupName=self.context.subnet_group_name,
                VpcSecurityGroupIds=[sg_id],
                ServerlessV2ScalingConfiguration={
                    "MinCapacity": self.context.min_capacity,
                    "MaxCapacity": self.context.max_capacity,
                },
                Tags=[{"Key": "Name", "Value": self.context.cluster_identifier}],
            )
            return True, None
        echo.log("Creating Aurora cluster: %s" % self.context.cluster_identifier)
        password: str | None = None
        password_kwargs: dict = {}
        if static:
            password = _generate_master_password()
            password_kwargs["MasterUserPassword"] = password
        else:
            password_kwargs["ManageMasterUserPassword"] = True
        self._rds_client.create_db_cluster(
            DBClusterIdentifier=self.context.cluster_identifier,
            Engine="aurora-postgresql",
            EngineMode="provisioned",
            DatabaseName=self.context.database_name,
            MasterUsername=self.context.master_username,
            DBSubnetGroupName=self.context.subnet_group_name,
            VpcSecurityGroupIds=[sg_id],
            ServerlessV2ScalingConfiguration={
                "MinCapacity": self.context.min_capacity,
                "MaxCapacity": self.context.max_capacity,
            },
            Tags=[{"Key": "Name", "Value": self.context.cluster_identifier}],
            **password_kwargs,
        )
        return True, password

    def _ensure_instance(self) -> None:
        """Aurora インスタンスを作成 (既存なら skip)。"""
        if self._describe_instance() is not None:
            echo.log(
                "Aurora instance %s already exists; skipping creation."
                % self.context.instance_identifier
            )
            return
        echo.log("Creating Aurora instance: %s" % self.context.instance_identifier)
        self._rds_client.create_db_instance(
            DBInstanceIdentifier=self.context.instance_identifier,
            DBClusterIdentifier=self.context.cluster_identifier,
            DBInstanceClass="db.serverless",
            Engine="aurora-postgresql",
            Tags=[{"Key": "Name", "Value": self.context.instance_identifier}],
        )

    def create(self):
        # VPC スタックの完了を待つ
        if not self.context.vpc:
            raise RuntimeError("vpc context is not configured")
        if self.context.vpc.manage:
            Vpc(self.context.vpc).stack.wait_status("COMPLETED")
        subnet_ids = self._get_vpc_subnet_ids()
        vpc_id = self._get_vpc_id()

        # 各ステップは「既に在れば再利用」で冪等にしてある。途中で失敗した
        # deploy の再実行や、一部リソースだけ先行作成済みのケースでも
        # AlreadyExists で落ちず、未完了のステップから続行できる。

        static = self.context.password_strategy == "static"  # noqa: S105 戦略名/保存先種別であって secret 値ではない

        # 1. DB Subnet Group / 2. Security Group
        self._ensure_subnet_group(subnet_ids)
        sg_id = self._ensure_security_group(vpc_id)

        # 3. Aurora クラスター (snapshot があれば復元)。cluster_created =
        # このセッションで新規作成したか。password 切替 modify や static secret の
        # 保存は「新規作成時のみ」実施する (再実行で static パスワードを作り直さない。
        # 既存クラスタの調整は update() が担う)。
        cluster_created, password = self._create_or_restore_cluster(sg_id, static)

        # 4. Aurora インスタンス
        self._ensure_instance()

        # 5. クラスター/インスタンス available を待機（最大30分）。
        # cluster available だけでは instance がまだ creating のことがあるため、
        # 後続の modify_db_cluster (password 切替) の前に instance available まで
        # 待つ。待たないと restore 直後の modify が反映されない/失敗しうる。
        echo.log("Waiting for Aurora cluster to become available...")
        self._wait_cluster_available(timeout=1800)
        echo.log("Waiting for Aurora instance to become available...")
        self._wait_instance_available(timeout=1800)

        # 6. snapshot から復元した場合、マスターパスワードを設定し直す。
        # (RestoreDBClusterFromSnapshot は snapshot の元パスワードを引き継ぐため、
        # pocket が参照できる認証情報を改めて確立する必要がある)
        # 新規に復元したときだけ実施 (再実行時の再切替を避ける)。
        if cluster_created and self.context.snapshot_identifier:
            if static:
                echo.log("Setting a pocket-managed static master password...")
                password = _generate_master_password()
                self._rds_client.modify_db_cluster(
                    DBClusterIdentifier=self.context.cluster_identifier,
                    MasterUserPassword=password,
                    ApplyImmediately=True,
                )
            else:
                echo.log(
                    "Switching master password to AWS-managed secret "
                    "(ManageMasterUserPassword=True)..."
                )
                self._rds_client.modify_db_cluster(
                    DBClusterIdentifier=self.context.cluster_identifier,
                    ManageMasterUserPassword=True,
                    ApplyImmediately=True,
                )
            self._wait_cluster_available(timeout=600)

        # 7. static: 生成したパスワードを pocket 所有の secret に保存する。
        # この secret を MasterUserSecret 相当として Lambda へ渡すため、ローテーション
        # 用 Lambda は付けない (= 自動ローテーションしない)。新規作成時のみ。
        if cluster_created and static:
            if password is None:
                raise RuntimeError("password must be set for static credentials")
            self._store_static_credentials(password)

        echo.success("Aurora cluster is now available.")

    def _store_static_credentials(self, password: str) -> None:
        """static パスワードと接続情報を pocket 所有の store に保存する。

        保存先は awscontainer.secrets.store のトグル (sm / ssm) に従う。
        MasterUserSecret (ManageMasterUserPassword) と同じ username/password 形に
        host/port/dbname も加えるため、Lambda 側は環境変数フォールバック無しでも
        DATABASE_URL を組み立てられる。ローテーション用 Lambda は付けない。
        """
        self.clear_cache()  # endpoint/port を最新のクラスタ情報から取得する
        secret_string = json.dumps(
            {
                "username": self.context.master_username,
                "password": password,
                "host": self.endpoint,
                "port": self.port,
                "dbname": self.database_name,
                "engine": "postgres",
                "dbClusterIdentifier": self.context.cluster_identifier,
            }
        )
        self._write_credential_to_store(self.context.secret_store, secret_string)
        self.clear_cache()

    def _write_credential_to_store(self, store: str, secret_string: str) -> None:
        name = self.context.credentials_secret_name
        result = secret_store.put_stored_value(
            name, store, secret_string, self.context.region
        )
        if store == "ssm":
            echo.success("Stored static DB credentials in SSM parameter: %s" % name)
        elif result is secret_store.PutResult.CREATED:
            echo.success("Stored static DB credentials secret: %s" % name)
        else:
            echo.success("Updated static DB credentials secret: %s" % name)

    def _read_credential_from_store(self, store: str) -> str | None:
        return secret_store.read_stored_value(
            self.context.credentials_secret_name, store, self.context.region
        )

    def _delete_credential_from_store(self, store: str) -> None:
        secret_store.delete_stored_value(
            self.context.credentials_secret_name,
            store,
            self.context.region,
            force_sm=True,
            swallow_not_found=True,
        )

    def update(self):
        self.clear_cache()
        scaling_changed = not self._scaling_config_matches()
        if scaling_changed:
            echo.log("Updating RDS scaling configuration...")
            self._rds_client.modify_db_cluster(
                DBClusterIdentifier=self.context.cluster_identifier,
                ServerlessV2ScalingConfiguration={
                    "MinCapacity": self.context.min_capacity,
                    "MaxCapacity": self.context.max_capacity,
                },
            )
            echo.success("RDS scaling configuration updated.")
        if not self._password_state_matches():
            if scaling_changed:
                self._wait_cluster_available(timeout=600)
            self._migrate_password()

    def _migrate_password(self) -> None:
        """クラスタを望む password_strategy / secret_store に合わせて移行する。"""
        if self.context.password_strategy == "aws-managed":  # noqa: S105 戦略名/保存先種別であって secret 値ではない
            self._migrate_to_managed()
        else:
            self._migrate_to_static()

    def _migrate_to_managed(self) -> None:
        echo.log("Migrating master password to AWS-managed...")
        self._rds_client.modify_db_cluster(
            DBClusterIdentifier=self.context.cluster_identifier,
            ManageMasterUserPassword=True,
            ApplyImmediately=True,
        )
        self._wait_cluster_available(timeout=600)
        # pocket 所有の認証情報は不要になるので両 store から除去
        for store in ("sm", "ssm"):
            self._delete_credential_from_store(store)
        self.clear_cache()
        echo.success("Master password is now AWS-managed.")

    def _migrate_to_static(self) -> None:
        if self._actual_is_managed():
            # aws-managed → static: managed を切り、既知パスワードを設定。
            # (Manage=False + MasterUserPassword 指定で RDS が managed secret を削除)
            echo.log("Migrating master password to pocket-managed static...")
            password = _generate_master_password()
            self._rds_client.modify_db_cluster(
                DBClusterIdentifier=self.context.cluster_identifier,
                ManageMasterUserPassword=False,
                MasterUserPassword=password,
                ApplyImmediately=True,
            )
            self._wait_cluster_available(timeout=600)
            self._store_static_credentials(password)
            echo.success("Master password is now pocket-managed (static).")
            return
        # クラスタは既に static。store 変更 (sm⇄ssm) を試みる (パスワード変更なし)。
        other = "sm" if self.context.secret_store == "ssm" else "ssm"  # noqa: S105 戦略名/保存先種別であって secret 値ではない
        existing = self._read_credential_from_store(other)
        if existing is not None:
            echo.log(
                "Moving static DB credentials to %s store..."
                % (self.context.secret_store)
            )
            self._write_credential_to_store(self.context.secret_store, existing)
            self._delete_credential_from_store(other)
            self.clear_cache()
            return
        # どの store にも認証情報が無い: パスワードを再設定して保存し直す。
        echo.warning(
            "Static credential not found in any store; resetting master password."
        )
        password = _generate_master_password()
        self._rds_client.modify_db_cluster(
            DBClusterIdentifier=self.context.cluster_identifier,
            MasterUserPassword=password,
            ApplyImmediately=True,
        )
        self._wait_cluster_available(timeout=600)
        self._store_static_credentials(password)

    def delete(self):
        # 1. インスタンス削除
        if self.instance:
            echo.log("Deleting Aurora instance: %s" % self.context.instance_identifier)
            self._rds_client.delete_db_instance(
                DBInstanceIdentifier=self.context.instance_identifier,
                SkipFinalSnapshot=True,
            )
            self._wait_instance_deleted(timeout=600)
            echo.success("Aurora instance deleted.")

        # 2. クラスター削除（FinalSnapshot 付き）
        if self.cluster:
            snapshot_id = "%s-final-%s" % (
                self.context.cluster_identifier,
                int(time.time()),
            )
            echo.log("Deleting Aurora cluster: %s" % self.context.cluster_identifier)
            self._rds_client.delete_db_cluster(
                DBClusterIdentifier=self.context.cluster_identifier,
                SkipFinalSnapshot=False,
                FinalDBSnapshotIdentifier=snapshot_id,
            )
            self._wait_cluster_deleted(timeout=600)
            echo.success("Aurora cluster deleted. Final snapshot: %s" % snapshot_id)

        # 3. Security Group 削除
        if self.security_group_id:
            echo.log("Deleting Security Group: %s" % self.context.security_group_name)
            self._ec2_client.delete_security_group(GroupId=self.security_group_id)
            echo.success("Security Group deleted.")

        # 4. Subnet Group 削除
        try:
            echo.log("Deleting DB Subnet Group: %s" % self.context.subnet_group_name)
            self._rds_client.delete_db_subnet_group(
                DBSubnetGroupName=self.context.subnet_group_name
            )
            echo.success("DB Subnet Group deleted.")
        except ClientError as e:
            if e.response["Error"]["Code"] != "DBSubnetGroupNotFoundFault":
                raise

        # 5. static: pocket 所有の認証情報を store から削除
        if self.context.password_strategy == "static":  # noqa: S105 戦略名/保存先種別であって secret 値ではない
            self._delete_static_credentials()

    def _delete_static_credentials(self) -> None:
        """static の認証情報を保存先 store (sm / ssm) から削除する。"""
        name = self.context.credentials_secret_name
        echo.log(
            "Deleting static DB credentials (%s): %s"
            % (
                self.context.secret_store,
                name,
            )
        )
        self._delete_credential_from_store(self.context.secret_store)
        echo.success("Static DB credentials deleted.")

    def _wait_cluster_available(self, timeout: int = 1800, interval: int = 10):
        def poll():
            try:
                res = self._rds_client.describe_db_clusters(
                    DBClusterIdentifier=self.context.cluster_identifier
                )
                return res["DBClusters"][0]["Status"] == "available"
            except ClientError:
                return False

        wait_until(
            poll,
            timeout=timeout,
            interval=interval,
            start_message="Waiting for cluster to be available",
            timeout_message=(
                "Cluster did not become available within %s seconds" % timeout
            ),
        )

    def _wait_instance_available(self, timeout: int = 1800, interval: int = 10):
        def poll():
            try:
                res = self._rds_client.describe_db_instances(
                    DBInstanceIdentifier=self.context.instance_identifier
                )
                return res["DBInstances"][0]["DBInstanceStatus"] == "available"
            except ClientError:
                return False

        wait_until(
            poll,
            timeout=timeout,
            interval=interval,
            start_message="Waiting for instance to be available",
            timeout_message=(
                "Instance did not become available within %s seconds" % timeout
            ),
        )

    def _wait_instance_deleted(self, timeout: int = 600, interval: int = 10):
        def poll():
            try:
                self._rds_client.describe_db_instances(
                    DBInstanceIdentifier=self.context.instance_identifier
                )
                return False
            except ClientError as e:
                if e.response["Error"]["Code"] == "DBInstanceNotFound":
                    return True
                raise

        wait_until(
            poll,
            timeout=timeout,
            interval=interval,
            start_message="Waiting for instance deletion",
            timeout_message="Instance not deleted within %s seconds" % timeout,
        )

    def _wait_cluster_deleted(self, timeout: int = 600, interval: int = 10):
        def poll():
            try:
                self._rds_client.describe_db_clusters(
                    DBClusterIdentifier=self.context.cluster_identifier
                )
                return False
            except ClientError as e:
                if e.response["Error"]["Code"] == "DBClusterNotFoundFault":
                    return True
                raise

        wait_until(
            poll,
            timeout=timeout,
            interval=interval,
            start_message="Waiting for cluster deletion",
            timeout_message="Cluster not deleted within %s seconds" % timeout,
        )

    def clear_cache(self):
        for attr in ("cluster", "instance", "_security_group", "_static_secret"):
            if attr in self.__dict__:
                del self.__dict__[attr]
