"""AWS Backup の前提リソース (vault / サービスロール) の共有 ensure。

定期バックアップ (resources/backup.py の Backup) と dsql のオンデマンド
バックアップ (resources/dsql.py) の両方が使うため、どちらからも import できる
場所に置く (backup.py は Dsql / Rds を import するので、dsql.py が backup.py を
import すると循環になる)。
"""

from __future__ import annotations

import json
import time

from botocore.exceptions import ClientError

from pocket.utils import echo

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


def ensure_backup_vault(backup_client, name: str) -> None:
    """pocket 管理の vault を冪等に確保する。

    describe → 無ければ create の順は不可。vault が 1 つも無いアカウントでは
    AWS Backup が存在しない vault への Describe に ResourceNotFoundException
    ではなく AccessDeniedException を返すため「未作成」を判定できない。
    CreateBackupVault は同名 vault があると AlreadyExistsException を返すので
    create を先に撃って握る (既存なら noop = 冪等)。
    """
    try:
        backup_client.create_backup_vault(BackupVaultName=name)
    except ClientError as e:
        if e.response["Error"]["Code"] == "AlreadyExistsException":
            return
        raise
    echo.log("Created backup vault: %s" % name)


def ensure_backup_role(iam_client, permissions_boundary: str | None) -> str:
    """AWS Backup サービスロールを冪等に ensure して ARN を返す。

    codebuild の _ensure_role と同型。boundary 必須のアカウントでも作成
    できるよう permissions_boundary を付与する。
    """
    try:
        return iam_client.get_role(RoleName=BACKUP_ROLE_NAME)["Role"]["Arn"]
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
    if permissions_boundary:
        create_kwargs["PermissionsBoundary"] = permissions_boundary
    role_arn: str = iam_client.create_role(**create_kwargs)["Role"]["Arn"]
    for policy_arn in _BACKUP_ROLE_POLICIES:
        iam_client.attach_role_policy(RoleName=BACKUP_ROLE_NAME, PolicyArn=policy_arn)
    # IAM ロールの伝播待ち (作成直後の StartBackupJob 失敗を避ける)
    echo.log("Waiting for IAM role propagation...")
    time.sleep(10)
    return role_arn
