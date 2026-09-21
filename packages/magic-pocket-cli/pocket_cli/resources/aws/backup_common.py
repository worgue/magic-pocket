"""AWS Backup の前提リソース (vault) の共有 ensure。

サービスロールは他の role と同じく iam_roles.py (backup_role) で扱う。

定期バックアップ (resources/backup.py の Backup) と dsql のオンデマンド
バックアップ (resources/dsql.py) の両方が使うため、どちらからも import できる
場所に置く (backup.py は Dsql / Rds を import するので、dsql.py が backup.py を
import すると循環になる)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

from pocket.utils import echo

if TYPE_CHECKING:
    from mypy_boto3_backup import BackupClient


# pocket 管理のバックアップ前提リソース。AWS Backup の Default vault /
# AWSBackupDefaultServiceRole は console 初回操作で作られるもので、API しか
# 使わないアカウントには存在しないため、pocket 側で冪等に ensure する。
# 名前は stage 単位 ({resource_prefix}backup / {resource_prefix}backup-role) で、
# context が導出する。
#
# 0.36 以前は account 共有の固定名だった。vault は改名も recovery point の移動も
# できず、account 内の他 stage / project と共有なので pocket からは消せない。
# 旧 vault の recovery point は取得時の lifecycle で失効するまで残り、pocket の
# list / cleanup の対象外になる (docs の移行手順を参照)
LEGACY_BACKUP_VAULT_NAME = "pocket-backup"
LEGACY_BACKUP_ROLE_NAME = "forge-pocket-backup-role"


def ensure_backup_vault(backup_client: BackupClient, name: str) -> None:
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
