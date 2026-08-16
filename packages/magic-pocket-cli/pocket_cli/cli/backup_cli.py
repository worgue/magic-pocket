import click

from pocket.context import Context
from pocket.utils import echo
from pocket_cli.resources.backup import Backup


def _get_backup_resource(stage: str) -> Backup:
    context = Context.from_toml(stage=stage)
    if context.backup is None:
        raise click.ClickException(
            "この stage には AWS Backup の対象 DB (dsql / managed rds) がありません。"
        )
    if not context.backup.declared:
        raise click.ClickException(
            "[backup] が宣言されていません。バックアップデータの削除には"
            " [backup] の deletable = true 宣言が必要です。"
        )
    if not context.backup.deletable:
        raise click.ClickException(
            "バックアップデータ (recovery point) の削除には [backup] に"
            " deletable = true の宣言が必要です。誤操作でデータを失わないための"
            " ガードなので、削除する時だけ宣言することを推奨します。"
        )
    return Backup(context.backup, dsql_context=context.dsql, rds_context=context.rds)


@click.group()
def backup():
    pass


@backup.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="確認プロンプトをスキップ"
)
def cleanup(stage: str, yes: bool):
    """この stage のバックアップデータ (recovery point) を削除する。

    [backup] deletable = true の宣言が必要。pocket 管理 vault (pocket-backup)
    にある、現存する対象 DB (dsql / managed rds) の recovery point だけを消す。
    削除済み cluster の分は ARN で引けないため対象外 (AWS Backup コンソールから
    削除する)。plan (スケジュール) には触らない。
    """
    r = _get_backup_resource(stage)
    points = r.list_recovery_points()
    if points is None:
        raise click.ClickException(
            "recovery point を列挙する権限がありません (backup:*)。"
        )
    if not points:
        echo.info("削除対象の recovery point はありません。")
        return
    echo.warning("以下の recovery point を削除します:")
    for point in points:
        created = point.get("CreationDate")
        echo.info(
            "  - %s (%s)"
            % (point["RecoveryPointArn"], created.isoformat() if created else "-")
        )
    echo.danger("この操作は取り消せません！")
    if not yes:
        click.confirm(
            "%d 件の recovery point を削除しますか？" % len(points), abort=True
        )
    deleted = r.delete_recovery_points(points)
    echo.success("%d 件の recovery point を削除しました。" % deleted)
