"""pocket cleanup-deprecated: 旧バージョンが残した account 単位リソースの掃除。

stage 単位の移行 (`pocket migrate` / deploy の migrate フェーズ) と違い、account
内の全 stage・全 project が共有していたリソースを対象にする。どの stage からも
参照されなくなったことを検査した上で、利用者の明示操作でだけ削除する
(deploy から自動では呼ばない)。
"""

from __future__ import annotations

import time

import click

from pocket.settings import Settings
from pocket.utils import echo
from pocket_cli.cli import interaction
from pocket_cli.resources.aws.backup_common import (
    LEGACY_BACKUP_ROLE_NAME,
    LEGACY_BACKUP_VAULT_NAME,
)
from pocket_cli.resources.aws.backup_legacy import LegacyBackupCleanup

_VAULT_DELETE_ATTEMPTS = 12
_VAULT_DELETE_INTERVAL = 10


@click.command(name="cleanup-deprecated")
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option(
    "--delete-recovery-points",
    is_flag=True,
    default=False,
    help="旧 vault に残るバックアップデータ (recovery point) も削除する"
    " (account 内の他 project の分も含む)",
)
@click.option("--dry-run", is_flag=True, default=False, help="対象の表示だけ行う")
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="確認プロンプトをスキップ"
)
def cleanup_deprecated(
    stage: str, delete_recovery_points: bool, dry_run: bool, yes: bool
):
    """旧バージョンが残した account 共有リソースを削除する。

    対象は 0.36 以前の backup vault (pocket-backup) とサービスロール
    (forge-pocket-backup-role)。AWS account を共有している全 stage・全 project を
    0.37.0 以上で deploy し終えてから、account/region ごとに 1 回実行する
    (--stage は region の解決に使う)。旧リソースをまだ参照している plan /
    selection があれば中止する。IAM ロールは全 region 共通なので、複数 region で
    pocket を使っている場合は全 region の更新後に実行すること。
    """
    interaction.set_assume_yes(yes)
    region = Settings.from_toml(stage=stage).region
    cleanup = LegacyBackupCleanup(region)
    _abort_if_referenced(cleanup, region)

    vault_exists = cleanup.vault_exists()
    role_exists = cleanup.role_exists()
    if not vault_exists and not role_exists:
        echo.success("削除対象の旧リソースはありません (region: %s)。" % region)
        return

    points = cleanup.recovery_points() if vault_exists else []
    delete_vault = vault_exists and (not points or delete_recovery_points)
    if vault_exists:
        echo.info(
            "backup vault: %s (recovery point %d 件)"
            % (LEGACY_BACKUP_VAULT_NAME, len(points))
        )
    if role_exists:
        echo.info("IAM role: %s" % LEGACY_BACKUP_ROLE_NAME)
    if vault_exists and not delete_vault:
        _explain_kept_vault(cleanup, points)
    if dry_run or not (delete_vault or role_exists):
        return

    _confirm(points if delete_vault else [])
    if role_exists and cleanup.delete_role():
        echo.log("Deleted IAM role: %s" % LEGACY_BACKUP_ROLE_NAME)
    if delete_vault and not _delete_vault(cleanup, points):
        raise click.ClickException(
            "recovery point の削除がまだ反映されておらず、vault を削除できません"
            "でした。少し待って再実行してください。"
        )
    echo.success("旧リソースの掃除が完了しました。")


def _abort_if_referenced(cleanup: LegacyBackupCleanup, region: str) -> None:
    references = cleanup.references()
    if not references:
        return
    for ref in references:
        echo.warning("  - %s" % ref)
    raise click.ClickException(
        "旧リソースをまだ参照している設定があります (region: %s)。"
        "該当する stage を 0.37.0 以上の pocket で deploy してから"
        "再実行してください。" % region
    )


def _confirm(points_to_delete: list[dict]) -> None:
    if points_to_delete:
        text = (
            "旧 vault の recovery point %d 件を削除します。account 内の他 project の"
            "バックアップデータも含まれ、復元できなくなります。続行しますか？"
            % len(points_to_delete)
        )
    else:
        text = "上記の旧リソースを削除しますか？"
    interaction.confirm(text, default=False, abort=True)


def _delete_vault(cleanup: LegacyBackupCleanup, points: list[dict]) -> bool:
    if points:
        cleanup.delete_recovery_points(points)
        echo.log("Deleted %d recovery points" % len(points))
    # recovery point の削除は非同期で、直後の DeleteBackupVault は「空でない」と
    # 拒否される (実測)。反映を少し待つ
    for attempt in range(_VAULT_DELETE_ATTEMPTS):
        if cleanup.delete_vault():
            echo.log("Deleted backup vault: %s" % LEGACY_BACKUP_VAULT_NAME)
            return True
        if attempt + 1 < _VAULT_DELETE_ATTEMPTS:
            time.sleep(_VAULT_DELETE_INTERVAL)
    return False


def _explain_kept_vault(cleanup: LegacyBackupCleanup, points: list[dict]) -> None:
    expiry = cleanup.last_expiry(points)
    when = (
        "%s 以降" % expiry.strftime("%Y-%m-%d")
        if expiry
        else "無期限保持のものがあるため自動では空になりません"
    )
    echo.warning(
        "recovery point が残っているため vault は削除しません。保持期限で失効して"
        "空になった後 (%s) に再実行するか、--delete-recovery-points で"
        "データごと削除してください。" % when
    )
