"""0.36 以前の account 共有 backup リソース (vault / サービスロール) の掃除。

0.37.0 で vault / ロールを stage 単位の名前に改めたため、旧名のリソースは
どのコードからも参照されなくなる。ただし account 内の全 stage・全 project が
共有していたので、deploy や destroy (= stage 単位の操作) からは消せない。
「全 stage を更新し終えた」ことを利用者が確認した上で明示的に呼ぶ
`pocket cleanup-deprecated` がここを使う。
"""

from __future__ import annotations

from datetime import datetime

import boto3
from botocore.exceptions import ClientError

from pocket_cli.resources.aws import iam_roles
from pocket_cli.resources.aws.backup_common import (
    LEGACY_BACKUP_ROLE_NAME,
    LEGACY_BACKUP_VAULT_NAME,
)


class LegacyBackupCleanup:
    def __init__(self, region: str) -> None:
        self.region = region
        self._backup = boto3.client("backup", region_name=region)
        self._iam = boto3.client("iam", region_name=region)

    def references(self) -> list[str]:
        """旧 vault / 旧ロールをまだ参照している plan・selection の説明一覧。

        1 件でもあれば、その stage は 0.36 以前のまま (未 deploy) なので、
        消すと定期バックアップが失敗し始める。掃除の前提条件として検査する。
        """
        found: list[str] = []
        paginator = self._backup.get_paginator("list_backup_plans")
        for page in paginator.paginate():
            for item in page["BackupPlansList"]:
                plan_id = item["BackupPlanId"]
                plan_name = item["BackupPlanName"]
                plan = self._backup.get_backup_plan(BackupPlanId=plan_id)["BackupPlan"]
                if any(
                    rule.get("TargetBackupVaultName") == LEGACY_BACKUP_VAULT_NAME
                    for rule in plan.get("Rules", [])
                ):
                    found.append(
                        "backup plan %s (保存先が %s)"
                        % (plan_name, LEGACY_BACKUP_VAULT_NAME)
                    )
                found.extend(self._selection_references(plan_id, plan_name))
        return found

    def _selection_references(self, plan_id: str, plan_name: str) -> list[str]:
        found: list[str] = []
        suffix = ":role/%s" % LEGACY_BACKUP_ROLE_NAME
        selections = self._backup.list_backup_selections(BackupPlanId=plan_id)
        for item in selections["BackupSelectionsList"]:
            detail = self._backup.get_backup_selection(
                BackupPlanId=plan_id, SelectionId=item["SelectionId"]
            )
            if detail["BackupSelection"].get("IamRoleArn", "").endswith(suffix):
                found.append(
                    "backup selection %s / %s (ロールが %s)"
                    % (plan_name, item["SelectionName"], LEGACY_BACKUP_ROLE_NAME)
                )
        return found

    def vault_exists(self) -> bool:
        """旧 vault があるか。

        DescribeBackupVault は、vault が 1 つも無い account では存在しない vault に
        AccessDeniedException を返し「未作成」を判定できないため、一覧で調べる。
        """
        paginator = self._backup.get_paginator("list_backup_vaults")
        for page in paginator.paginate():
            for vault in page["BackupVaultList"]:
                if vault["BackupVaultName"] == LEGACY_BACKUP_VAULT_NAME:
                    return True
        return False

    def recovery_points(self) -> list[dict]:
        points: list[dict] = []
        paginator = self._backup.get_paginator("list_recovery_points_by_backup_vault")
        for page in paginator.paginate(BackupVaultName=LEGACY_BACKUP_VAULT_NAME):
            points.extend(page["RecoveryPoints"])
        return points

    @staticmethod
    def last_expiry(points: list[dict]) -> datetime | None:
        """全 recovery point が失効し終える日時 (無期限のものがあれば None)。"""
        expiries = [p.get("CalculatedLifecycle", {}).get("DeleteAt") for p in points]
        if not expiries or any(e is None for e in expiries):
            return None
        return max(expiries)

    def delete_recovery_points(self, points: list[dict]) -> None:
        for point in points:
            self._backup.delete_recovery_point(
                BackupVaultName=LEGACY_BACKUP_VAULT_NAME,
                RecoveryPointArn=point["RecoveryPointArn"],
            )

    def delete_vault(self) -> bool:
        """旧 vault を削除する。recovery point の削除が未反映で空でなければ False。"""
        try:
            self._backup.delete_backup_vault(BackupVaultName=LEGACY_BACKUP_VAULT_NAME)
        except ClientError as e:
            if e.response["Error"]["Code"] == "InvalidRequestException":
                return False
            raise
        return True

    def role_exists(self) -> bool:
        try:
            self._iam.get_role(RoleName=LEGACY_BACKUP_ROLE_NAME)
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchEntity":
                return False
            raise
        return True

    def delete_role(self) -> bool:
        return iam_roles.delete_role(
            self._iam, LEGACY_BACKUP_ROLE_NAME, iam_roles.BACKUP_ROLE_POLICIES
        )
