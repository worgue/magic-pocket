from __future__ import annotations

import json
from functools import cached_property
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from pocket.resources.base import ResourceStatus
from pocket.secret_store import (
    delete_stored_value,
    put_stored_value,
    read_stored_value,
)
from pocket.utils import echo
from pocket_cli.resources.aws.backup_common import (
    BACKUP_VAULT_NAME,
    ensure_backup_role,
    ensure_backup_vault,
)
from pocket_cli.resources.aws.poll import wait_until

if TYPE_CHECKING:
    from pocket.context import DsqlContext

# AWS Backup の backup job 終端状態。これ以外は進行中として扱う
BACKUP_TERMINAL_STATES = {"COMPLETED", "FAILED", "ABORTED", "EXPIRED", "PARTIAL"}
# restore job の終端状態
RESTORE_TERMINAL_STATES = {"COMPLETED", "FAILED", "ABORTED"}

# オンデマンドバックアップで保持日数の指定が無く [backup] も未宣言なときの既定。
# AWS Backup の vault は既定ライフサイクルを持たないため、Lifecycle を渡さないと
# recovery point は無期限に残る。pocket にも deploy role にもデータ削除権限が無く
# (destroy も recovery point を残す)、console 作業でしか消せなくなるため、
# 「いつかは失効する」ことを既定で保証する。3 年は復元要件として十分長く、
# 意図せず消えるより増え続けるほうが困る、という判断
DEFAULT_ON_DEMAND_RETENTION_DAYS = 1095


class Dsql:
    context: DsqlContext

    def __init__(self, context: DsqlContext) -> None:
        self.context = context
        self._client = boto3.client("dsql", region_name=context.region)
        # DSQL に組み込みの自動バックアップは無く、AWS Backup 統合が唯一の
        # バックアップ手段のため、オンデマンドバックアップをここで扱う
        self._backup = boto3.client("backup", region_name=context.region)
        self._iam = boto3.client("iam", region_name=context.region)

    @cached_property
    def cluster(self) -> dict | None:
        """Name タグで DSQL クラスターを検索"""
        paginator = self._client.get_paginator("list_clusters")
        for page in paginator.paginate():
            for cluster in page["clusters"]:
                identifier = cluster["identifier"]
                try:
                    detail = self._client.get_cluster(identifier=identifier)
                    tags = self._client.list_tags_for_resource(
                        resourceArn=detail["arn"]
                    )
                    if tags.get("tags", {}).get("Name") == self.context.tag_name:
                        return detail
                except ClientError:
                    continue
        return None

    @property
    def identifier(self) -> str | None:
        if self.cluster:
            return self.cluster["identifier"]
        return None

    @property
    def endpoint(self) -> str | None:
        if self.identifier:
            return f"{self.identifier}.dsql.{self.context.region}.on.aws"
        return None

    @property
    def arn(self) -> str | None:
        if self.cluster:
            return self.cluster["arn"]
        return None

    @property
    def status(self) -> ResourceStatus:
        if self.cluster is None:
            return "NOEXIST"
        cluster_status = self.cluster["status"]
        if cluster_status in ("CREATING", "UPDATING", "DELETING"):
            return "PROGRESS"
        if cluster_status == "ACTIVE":
            return "COMPLETED"
        return "FAILED"

    @property
    def description(self):
        return "Create Aurora DSQL cluster: %s" % self.context.tag_name

    def state_info(self):
        return {
            "dsql": {
                "tag_name": self.context.tag_name,
                "identifier": self.identifier,
                "endpoint": self.endpoint,
                "endpoint_secret_name": self.context.endpoint_secret_name,
            }
        }

    def deploy_init(self):
        # DSQL は他の DB backend と違い、組み込みの自動バックアップ (PITR /
        # 日次スナップショット) を一切持たない。deploy が黙って通ると「managed
        # DB だから守られている」と誤解されたまま、誤削除・論理破壊からの復元
        # 手段がゼロの状態で本番が動くため、宣言が無い stage では毎 deploy で
        # 明示する。
        if self.context.backup:
            return
        echo.warning(
            "DSQL には自動バックアップ (PITR / スナップショット) がありません。"
        )
        echo.warning(
            "  定期バックアップは [backup.dsql] を宣言してください。"
            " 単発なら `pocket resource dsql backup` で取得できます。"
        )

    def create(self):
        echo.log("Creating DSQL cluster: %s" % self.context.tag_name)
        res = self._client.create_cluster(
            deletionProtectionEnabled=self.context.deletion_protection,
            tags={"Name": self.context.tag_name},
        )
        identifier = res["identifier"]
        echo.log("Cluster ID: %s" % identifier)
        echo.log("Waiting for DSQL cluster to become active...")
        self._wait_active(identifier, timeout=600)
        self.clear_cache()
        echo.success("DSQL cluster is now active.")
        echo.success("Endpoint: %s" % self.endpoint)
        # ensure_post_deploy_state でも publish されるが、後続 resource の失敗で
        # hook まで到達しない deploy でも作成記録が残るよう、作成直後にも書く
        self.publish_endpoint()

    def delete(self):
        if not self.identifier:
            return
        echo.log("Deleting DSQL cluster: %s" % self.identifier)
        self._client.delete_cluster(identifier=self.identifier)
        echo.log("Waiting for DSQL cluster deletion...")
        self._wait_deleted(self.identifier, timeout=600)
        echo.success("DSQL cluster was deleted.")
        self._unpublish_endpoint()

    def ensure_post_deploy_state(self):
        # create の有無に関わらず毎 deploy で publish を冪等に確保する。cluster
        # 再作成や旧バージョンで deploy 済みの既存 cluster も常に実体を反映する
        # (定期バックアップの plan / selection は Backup resource の責務)
        self.publish_endpoint()

    @property
    def backup_target_arn(self) -> str | None:
        """[backup] の selection 対象 ARN (Backup resource が参照する)。"""
        return self.arn

    def publish_endpoint(self):
        """endpoint を stored user secret 正準パスへ publish する (作成記録)。

        DSQL は cluster identifier が AWS 自動生成のため endpoint を naming から
        導出できない。deploy の外の消費者 (migration ツール等) が endpoint を
        決定的に引けるよう、作成者である deploy が正準パスへ書き込む。
        値が既に一致していれば書き込まない (SSM version / SM stage の増殖防止)。
        """
        name = self.context.endpoint_secret_name
        endpoint = self.endpoint
        if not name or endpoint is None:
            return
        store = self.context.endpoint_secret_store
        current = read_stored_value(name, store, self.context.region)
        if current == endpoint:
            return
        put_stored_value(name, store, endpoint, self.context.region)
        echo.log("Published DSQL endpoint to %s" % name)

    def _unpublish_endpoint(self):
        """publish 済み endpoint を削除する (cluster 削除との対称操作)。

        残すと削除済み cluster の endpoint を消費者が読み続けるため、削除まで
        deploy 側の責務とする。未 publish (NotFound) は握りつぶす。
        """
        name = self.context.endpoint_secret_name
        if not name:
            return
        delete_stored_value(
            name,
            self.context.endpoint_secret_store,
            self.context.region,
            force_sm=True,
            swallow_not_found=True,
        )
        echo.log("Removed published DSQL endpoint: %s" % name)

    def start_backup(
        self,
        vault_name: str | None = None,
        iam_role_arn: str | None = None,
        retention_days: int | None = None,
    ) -> str:
        """AWS Backup のオンデマンドバックアップを開始し job id を返す。

        vault / ロール省略時は pocket 管理のものを冪等に ensure して使う
        (明示指定されたものは呼び出し側の管理物とみなし存在確認しない)。
        AWS Backup の service model は dsql と異なり PascalCase。
        """
        if not self.arn:
            raise ValueError("Cluster not found")
        if vault_name is None:
            vault_name = BACKUP_VAULT_NAME
            self._ensure_backup_vault(vault_name)
        if iam_role_arn is None:
            iam_role_arn = self._ensure_backup_role()
        params: dict = {
            "BackupVaultName": vault_name,
            "ResourceArn": self.arn,
            "IamRoleArn": iam_role_arn,
        }
        resolved = self._resolve_retention_days(retention_days)
        if resolved is not None:
            params["Lifecycle"] = {"DeleteAfterDays": resolved}
        res = self._backup.start_backup_job(**params)
        return res["BackupJobId"]

    def _resolve_retention_days(self, retention_days: int | None) -> int | None:
        """オンデマンドバックアップの保持日数を決める (None を返すと無期限)。

        指定 > [backup.dsql] の最長階層 (monthly) > 既定 の順で解決する。
        復元時の内部バックアップ (利用者が明示的に頼んでいない recovery point)
        も start_backup を通るため同じ既定が効く。0 は「無期限」の明示指定。
        """
        if retention_days == 0:
            echo.warning(
                "この recovery point は無期限保持です。"
                "pocket からは削除できないため、消すには console 作業が必要です。"
            )
            return None
        if retention_days is not None:
            echo.log("Retention: %d days (--retention-days)" % retention_days)
            return retention_days
        backup = self.context.backup
        if backup is not None:
            echo.log(
                "Retention: %d days (from [backup.dsql] monthly)"
                % backup.monthly_delete_after_days
            )
            return backup.monthly_delete_after_days
        echo.log("Retention: %d days (default)" % DEFAULT_ON_DEMAND_RETENTION_DAYS)
        return DEFAULT_ON_DEMAND_RETENTION_DAYS

    def _ensure_backup_vault(self, name: str) -> None:
        ensure_backup_vault(self._backup, name)

    def _ensure_backup_role(self) -> str:
        return ensure_backup_role(self._iam, self.context.permissions_boundary)

    def latest_recovery_point(self) -> dict | None:
        """現用クラスターの最新 recovery point (完了済み) を返す。"""
        if not self.arn:
            return None
        points: list[dict] = []
        paginator = self._backup.get_paginator("list_recovery_points_by_resource")
        for page in paginator.paginate(ResourceArn=self.arn):
            points.extend(page["RecoveryPoints"])
        completed = [p for p in points if p.get("Status") == "COMPLETED"]
        if not completed:
            return None
        return max(completed, key=lambda p: p["CreationDate"])

    def start_restore(
        self, recovery_point_arn: str, iam_role_arn: str | None = None
    ) -> str:
        """recovery point から復元を開始し restore job id を返す。

        AWS Backup の DSQL 復元は常に**新しいクラスター**を作る (既存を上書き
        しない)。削除保護は指定しないと AWS 既定で ON になるため、pocket.toml の
        値を明示して現用クラスターと揃える。
        """
        res = self._backup.start_restore_job(
            RecoveryPointArn=recovery_point_arn,
            IamRoleArn=iam_role_arn or self._ensure_backup_role(),
            Metadata={
                "regionalConfig": json.dumps(
                    [
                        {
                            "region": self.context.region,
                            "isDeletionProtectionEnabled": (
                                self.context.deletion_protection
                            ),
                        }
                    ]
                )
            },
        )
        return res["RestoreJobId"]

    def get_restore_job(self, job_id: str) -> dict:
        return self._backup.describe_restore_job(RestoreJobId=job_id)

    def wait_restore(
        self, job_id: str, timeout: int = 3600, interval: int = 15
    ) -> dict:
        result: dict = {}

        def poll():
            job = self.get_restore_job(job_id)
            if job["Status"] in RESTORE_TERMINAL_STATES:
                result.update(job)
                return True
            return False

        wait_until(
            poll,
            timeout=timeout,
            interval=interval,
            start_message="Waiting for restore job to complete",
            timeout_message="Restore job did not complete within %s seconds" % timeout,
        )
        return result

    def switch_to_cluster(self, new_arn: str) -> None:
        """Name タグを復元先クラスターへ付け替える (現用クラスターの切り替え)。

        pocket は Name タグでクラスターを探すため、同じタグを持つクラスターが
        2 つあると deploy がどちらを掴むか不定になる。先に旧クラスターの Name を
        退避名へ書き換えてから新クラスターに付けることで、重複する瞬間を作らない。
        旧クラスターは削除しない (復元結果が期待どおりでなかったときの戻り先)。
        冪等: 既に切り替え済みなら旧 = 新となり付け替えは起きない。
        """
        old_arn = self.arn
        old_identifier = self.identifier
        if old_arn and old_arn != new_arn:
            self._client.tag_resource(
                resourceArn=old_arn,
                tags={
                    "Name": "%s-replaced-%s" % (self.context.tag_name, old_identifier)
                },
            )
            echo.log("Renamed previous cluster: %s" % old_identifier)
        self._client.tag_resource(
            resourceArn=new_arn, tags={"Name": self.context.tag_name}
        )
        self.clear_cache()

    def get_backup_job(self, job_id: str) -> dict:
        return self._backup.describe_backup_job(BackupJobId=job_id)

    def latest_backup_job(self) -> dict | None:
        """このクラスターを対象にした最新の backup job を返す。

        ListBackupJobs の並び順は保証されていないため CreationDate で選ぶ。
        """
        if not self.arn:
            return None
        jobs: list[dict] = []
        paginator = self._backup.get_paginator("list_backup_jobs")
        for page in paginator.paginate(ByResourceArn=self.arn):
            jobs.extend(page["BackupJobs"])
        if not jobs:
            return None
        return max(jobs, key=lambda j: j["CreationDate"])

    def wait_backup(self, job_id: str, timeout: int = 3600, interval: int = 15) -> dict:
        """backup job が終端状態になるまで待機し、最終の job 情報を返す。"""
        result: dict = {}

        def poll():
            job = self.get_backup_job(job_id)
            if job["State"] in BACKUP_TERMINAL_STATES:
                result.update(job)
                return True
            return False

        wait_until(
            poll,
            timeout=timeout,
            interval=interval,
            start_message="Waiting for backup job to complete",
            timeout_message=("Backup job did not complete within %s seconds" % timeout),
        )
        return result

    def _wait_active(self, identifier: str, timeout: int = 600, interval: int = 5):
        def poll():
            try:
                res = self._client.get_cluster(identifier=identifier)
                return res["status"] == "ACTIVE"
            except ClientError:
                return False

        wait_until(
            poll,
            timeout=timeout,
            interval=interval,
            start_message="Waiting for cluster to be active",
            timeout_message=(
                "Cluster did not become active within %s seconds" % timeout
            ),
        )

    def _wait_deleted(self, identifier: str, timeout: int = 600, interval: int = 5):
        def poll():
            try:
                self._client.get_cluster(identifier=identifier)
                return False
            except ClientError as e:
                if e.response["Error"]["Code"] == "ResourceNotFoundException":
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
        if "cluster" in self.__dict__:
            del self.__dict__["cluster"]
