from __future__ import annotations

import json
import time
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
from pocket_cli.resources.aws.poll import wait_until

if TYPE_CHECKING:
    from pocket.context import DsqlContext

# AWS Backup の backup job 終端状態。これ以外は進行中として扱う
BACKUP_TERMINAL_STATES = {"COMPLETED", "FAILED", "ABORTED", "EXPIRED", "PARTIAL"}

# pocket 管理のバックアップ前提リソース。AWS Backup の Default vault /
# AWSBackupDefaultServiceRole は console 初回操作で作られるもので、API しか
# 使わないアカウントには存在しないため、pocket 側で冪等に ensure する。
# ロール名の forge- prefix は codebuild ロールの命名に合わせている
BACKUP_VAULT_NAME = "pocket-backup"
BACKUP_ROLE_NAME = "forge-pocket-backup-role"
_BACKUP_ROLE_POLICIES = (
    "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup",
    "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForRestores",
)


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
        # 手段がゼロの状態で本番が動くため、毎 deploy で明示する。
        # backup 方針を pocket.toml で宣言できるようになったら、宣言済みの
        # stage では出さない条件付きに切り替える。
        echo.warning(
            "DSQL には自動バックアップ (PITR / スナップショット) がありません。"
        )
        echo.warning(
            "  誤削除・論理破壊に備えるには `pocket resource dsql backup` で"
            "定期的にバックアップを取得してください。"
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
        self.publish_endpoint()

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
        if retention_days is not None:
            params["Lifecycle"] = {"DeleteAfterDays": retention_days}
        res = self._backup.start_backup_job(**params)
        return res["BackupJobId"]

    def _ensure_backup_vault(self, name: str) -> None:
        try:
            self._backup.describe_backup_vault(BackupVaultName=name)
            return
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
        echo.log("Creating backup vault: %s" % name)
        self._backup.create_backup_vault(BackupVaultName=name)

    def _ensure_backup_role(self) -> str:
        """AWS Backup サービスロールを冪等に ensure して ARN を返す。

        codebuild の _ensure_role と同型。boundary 必須のアカウントでも作成
        できるよう context.permissions_boundary を付与する。
        """
        try:
            return self._iam.get_role(RoleName=BACKUP_ROLE_NAME)["Role"]["Arn"]
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchEntity":
                raise
        echo.log("Creating AWS Backup service role: %s" % BACKUP_ROLE_NAME)
        assume_role_policy = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "backup.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            }
        )
        create_kwargs: dict = {
            "RoleName": BACKUP_ROLE_NAME,
            "AssumeRolePolicyDocument": assume_role_policy,
        }
        if self.context.permissions_boundary:
            create_kwargs["PermissionsBoundary"] = self.context.permissions_boundary
        role_arn: str = self._iam.create_role(**create_kwargs)["Role"]["Arn"]
        for policy_arn in _BACKUP_ROLE_POLICIES:
            self._iam.attach_role_policy(
                RoleName=BACKUP_ROLE_NAME, PolicyArn=policy_arn
            )
        # IAM ロールの伝播待ち (作成直後の StartBackupJob 失敗を避ける)
        echo.log("Waiting for IAM role propagation...")
        time.sleep(10)
        return role_arn

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
