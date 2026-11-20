import json
import re

import click
from botocore.exceptions import ClientError

from pocket.context import Context
from pocket.secret_store import read_stored_value
from pocket.utils import echo
from pocket_cli.cli.resource_helper import require_configured
from pocket_cli.resources.dsql import (
    BACKUP_TERMINAL_STATES,
    DEFAULT_ON_DEMAND_RETENTION_DAYS,
    RESTORE_TERMINAL_STATES,
    Dsql,
)


def _get_dsql_resource(stage: str) -> Dsql:
    context = Context.from_toml(stage=stage)
    return Dsql(
        require_configured(context.dsql, "dsql is not configured for this stage")
    )


@click.group()
def dsql():
    pass


@dsql.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def status(stage):
    """クラスター状態表示"""
    r = _get_dsql_resource(stage)
    echo.info("Tag Name: %s" % r.context.tag_name)
    echo.info("Status: %s" % r.status)
    if r.identifier:
        echo.info("Identifier: %s" % r.identifier)


@dsql.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option(
    "--format",
    "format_",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="出力形式。text: 人間向け (stderr・色付き) / json: 機械可読 (stdout)",
)
def endpoint(stage, format_):
    """接続情報表示"""
    r = _get_dsql_resource(stage)
    if not r.cluster:
        if format_ == "json":
            # スクリプト向け: 見つからないときは exit 1 で失敗を伝える
            # (text は従来通り warning + exit 0 のまま)
            raise click.ClickException("Cluster not found")
        echo.warning("Cluster not found")
        return
    if format_ == "json":
        click.echo(
            json.dumps(
                {
                    "endpoint": r.endpoint,
                    "region": r.context.region,
                    "port": 5432,
                }
            )
        )
        return
    _print_endpoint(r)


@dsql.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option(
    "--vault",
    default=None,
    help="バックアップ先の AWS Backup vault 名"
    " (省略時: pocket 管理の '{stage}-{project}-...-backup' を自動作成して使用)",
)
@click.option(
    "--iam-role-arn",
    default=None,
    help="AWS Backup が assume するロール ARN"
    " (省略時: pocket 管理の '{stage}-{project}-...-backup-role' を自動作成して使用)",
)
@click.option(
    "--retention-days",
    type=click.IntRange(min=0),
    default=None,
    help="recovery point の保持日数"
    " (省略時: [backup] の delete_after_days、宣言が無ければ %d 日。"
    "0 を指定すると無期限保持だが pocket からは削除できなくなる)"
    % DEFAULT_ON_DEMAND_RETENTION_DAYS,
)
@click.option("--watch", is_flag=True, help="バックアップ完了まで待機する")
def backup(stage, vault, iam_role_arn, retention_days, watch):
    """オンデマンドバックアップ (AWS Backup) を開始"""
    r = _get_dsql_resource(stage)
    if not r.arn:
        raise click.ClickException("Cluster not found")
    try:
        job_id = r.start_backup(
            vault, iam_role_arn=iam_role_arn, retention_days=retention_days
        )
    except ClientError as e:
        # AWS のエラー文言は API 名を含まないことがある (例: AccessDenied の
        # "Insufficient privileges...") ため、どの呼び出しで落ちたかを明示する
        err = e.response["Error"]
        raise click.ClickException(
            "%s (API: %s): %s" % (err["Code"], e.operation_name, err["Message"])
        ) from e
    echo.success("Backup job started: %s" % job_id)
    echo.info("Status check:")
    echo.info(
        "  pocket resource dsql backup-status --stage=%s --job-id=%s --watch"
        % (stage, job_id)
    )
    echo.info(
        "  aws backup describe-backup-job --backup-job-id %s --region %s"
        % (job_id, r.context.region)
    )
    if watch:
        _finish_backup_watch(r, job_id)


@dsql.command("backup-status")
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option(
    "--job-id",
    default=None,
    help="対象の backup job (省略時: このクラスターの最新 job)",
)
@click.option("--watch", is_flag=True, help="終端状態になるまで待機する")
def backup_status(stage, job_id, watch):
    """バックアップ job の状態確認"""
    r = _get_dsql_resource(stage)
    if job_id is None:
        job = r.latest_backup_job()
        if job is None:
            raise click.ClickException("No backup jobs found for this cluster")
        job_id = job["BackupJobId"]
    else:
        job = r.get_backup_job(job_id)
    _print_backup_job(job)
    if watch and job["State"] not in BACKUP_TERMINAL_STATES:
        _finish_backup_watch(r, job_id)


def _print_backup_job(job: dict):
    echo.info("Job ID: %s" % job["BackupJobId"])
    echo.info("State: %s" % job["State"])
    if job.get("PercentDone"):
        echo.info("Progress: %s%%" % job["PercentDone"])
    if job.get("StatusMessage"):
        echo.info("Message: %s" % job["StatusMessage"])


def _finish_backup_watch(r: Dsql, job_id: str):
    job = r.wait_backup(job_id)
    if job["State"] == "COMPLETED":
        echo.success("Backup completed: %s" % job.get("RecoveryPointArn", ""))
        return
    raise click.ClickException(
        "Backup job finished with state %s: %s"
        % (job["State"], job.get("StatusMessage", "(no message)"))
    )


@dsql.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.argument("recovery_point_arn", required=False)
@click.option(
    "--latest", is_flag=True, help="このクラスターの最新バックアップから復元する"
)
@click.option("--yes", is_flag=True, help="確認プロンプトを自動承認する")
@click.option(
    "--skip-backup", is_flag=True, help="復元前に現用クラスターのバックアップを取らない"
)
@click.option(
    "--resume",
    is_flag=True,
    help="中断した復元を再開する"
    " (新しい job は始めず、--job-id の完了を待って切り替える)",
)
@click.option(
    "--job-id", default=None, help="再開する restore job ID (--resume と一緒に指定する)"
)
def restore(stage, recovery_point_arn, latest, yes, skip_backup, resume, job_id):
    """バックアップから復元し、現用クラスターを切り替える"""
    _check_restore_options(recovery_point_arn, latest, skip_backup, resume, job_id)
    if resume:
        _resume_restore(_get_dsql_resource(stage), stage, job_id, yes)
        return
    r = _get_dsql_resource(stage)
    recovery_point_arn = _resolve_recovery_point(r, recovery_point_arn, latest)
    echo.info("Restore from: %s" % recovery_point_arn)
    if r.identifier:
        echo.info("Current cluster: %s" % r.identifier)

    if not skip_backup and r.arn:
        # 復元結果が期待どおりでなかったときの唯一の戻り先になるため、
        # 切り替え前に現用クラスターを固めておく
        if yes or click.confirm("Backup current cluster?", default=True):
            job_id = r.start_backup()
            echo.log("Backup job started: %s" % job_id)
            job = r.wait_backup(job_id)
            if job["State"] != "COMPLETED":
                raise click.ClickException(
                    "現用クラスターのバックアップが %s で終了しました: %s"
                    % (job["State"], job.get("StatusMessage", "(no message)"))
                )
            echo.success("Current cluster backed up.")

    job_id = r.start_restore(recovery_point_arn)
    echo.success("Restore job started: %s" % job_id)
    echo.info("If interrupted, resume with: %s" % _resume_command(stage, job_id))
    _finish_restore(r, stage, job_id)


def _check_restore_options(recovery_point_arn, latest, skip_backup, resume, job_id):
    """restore の引数の組み合わせを検査する (AWS を読む前に止める)。

    recovery point と --latest の組み合わせは _resolve_recovery_point が見る。
    """
    if not resume:
        if job_id is not None:
            raise click.ClickException("--job-id は --resume と一緒に指定してください")
        return
    if recovery_point_arn or latest or skip_backup:
        raise click.ClickException(
            "--resume は recovery point の指定・--latest・--skip-backup"
            " と同時に使えません"
        )
    if job_id is None:
        raise click.ClickException("--resume には --job-id が必要です")


def _resume_restore(r: Dsql, stage: str, job_id: str, yes: bool):
    """中断した restore の続き。新しい job は始めず、job の完了を待って切り替える。

    現用が job の cluster と違うときは確認を出す (過去の job の ID を渡した取り違えで
    現用を戻さないため)。現用の backup は取らない (最初の restore で済んでいる)。
    """
    echo.info("Job ID: %s" % job_id)
    _finish_restore(r, stage, job_id, confirm=not yes)


def _resume_command(stage: str, job_id: str) -> str:
    return "pocket resource dsql restore --stage=%s --resume --job-id=%s" % (
        stage,
        job_id,
    )


def _identifier_of(arn: str) -> str:
    """復元先 cluster の identifier (ARN の末尾 = ...:cluster/<id>)"""
    return re.split(r"[/:]", arn)[-1]


def _endpoint_published(r: Dsql) -> bool:
    """endpoint が正準名の stored secret に公開済みか (公開先が無ければ True)"""
    name = r.context.endpoint_secret_name
    if not name:
        return True
    current = read_stored_value(name, r.context.endpoint_secret_store, r.context.region)
    return current == r.endpoint


@dsql.command("restore-status")
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--job-id", required=True, help="restore job ID")
def restore_status(stage, job_id):
    """復元 job の状態確認（読むだけ。切り替えは restore --resume）"""
    r = _get_dsql_resource(stage)
    job = r.get_restore_job(job_id)
    echo.info("Job ID: %s" % job_id)
    echo.info("Status: %s" % job["Status"])
    if job.get("StatusMessage"):
        echo.info("Message: %s" % job["StatusMessage"])
    if job["Status"] != "COMPLETED":
        if job["Status"] not in RESTORE_TERMINAL_STATES:
            echo.info(
                "Restore job is still running. To wait and switch: %s"
                % _resume_command(stage, job_id)
            )
        return
    new_arn = job.get("CreatedResourceArn")
    if not new_arn:
        raise click.ClickException(
            "restore job は完了しましたが復元先 ARN を取得できませんでした: %s" % job_id
        )
    echo.info("Restored cluster: %s" % _identifier_of(new_arn))
    if r.arn == new_arn:
        echo.success("The restored cluster is current.")
        if not _endpoint_published(r):
            echo.warning(
                "endpoint が %s に公開されていません。公開するには: %s"
                % (r.context.endpoint_secret_name, _resume_command(stage, job_id))
            )
        return
    echo.info("Current cluster: %s" % (r.identifier or "(none)"))
    echo.warning("The restored cluster is not current.")
    echo.info("To switch: %s" % _resume_command(stage, job_id))


def _resolve_recovery_point(r: Dsql, recovery_point_arn, latest) -> str:
    if recovery_point_arn and latest:
        raise click.ClickException(
            "recovery point の指定と --latest は同時に使えません"
        )
    if recovery_point_arn:
        return recovery_point_arn
    if not latest:
        raise click.ClickException(
            "復元元の recovery point ARN を指定するか --latest を付けてください"
        )
    point = r.latest_recovery_point()
    if point is None:
        raise click.ClickException(
            "このクラスターの完了済みバックアップが見つかりません"
            " (クラスターが存在しない場合は recovery point ARN を直接指定してください)"
        )
    echo.info("Latest recovery point: %s" % point["CreationDate"])
    return point["RecoveryPointArn"]


def _finish_restore(r: Dsql, stage: str, job_id: str, confirm: bool = False):
    """restore job の完了を待ち、現用クラスターの切り替えまで行う。

    切り替え (Name タグの付け替えと endpoint の publish) は冪等なので、待機が
    中断された後に restore --resume から再実行しても安全。confirm なら、現用が
    job の cluster と違うときに確認する (restore --resume)。
    """
    job = r.wait_restore(job_id)
    if job["Status"] != "COMPLETED":
        raise click.ClickException(
            "Restore job finished with status %s: %s"
            % (job["Status"], job.get("StatusMessage", "(no message)"))
        )
    new_arn = job.get("CreatedResourceArn")
    if not new_arn:
        raise click.ClickException(
            "restore job は完了しましたが復元先 ARN を取得できませんでした: %s" % job_id
        )
    if confirm and r.arn != new_arn:
        echo.info("Current cluster: %s" % (r.identifier or "(none)"))
        echo.info("Switch to: %s" % _identifier_of(new_arn))
        click.confirm(
            "Switch the current cluster to the restored cluster?",
            default=False,
            abort=True,
        )
    previous_identifier = r.identifier
    r.switch_to_cluster(new_arn)
    r.publish_endpoint()
    echo.success("Restored cluster is now current: %s" % r.identifier)
    echo.success("Endpoint: %s" % r.endpoint)
    echo.warning(
        "まだアプリは切り替わっていません。`pocket deploy --stage=%s` を実行するまで、"
        "Lambda は旧クラスターに書き込み続けます"
        " (endpoint と IAM ポリシーが CloudFormation 管理のため)。" % stage
    )
    if previous_identifier and previous_identifier != r.identifier:
        echo.warning(
            "旧クラスター %s は削除していません。明示的に削除するまで課金され続けます"
            " (戻り先として残しています)。" % previous_identifier
        )


@dsql.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def destroy(stage):
    """確認付き削除"""
    r = _get_dsql_resource(stage)
    if r.status == "NOEXIST":
        echo.info("DSQL cluster does not exist.")
        return
    click.confirm(
        "DSQL クラスター '%s' を削除しますか？" % r.context.tag_name,
        abort=True,
    )
    r.delete()


def _print_endpoint(r: Dsql):
    echo.success("Endpoint: %s" % r.endpoint)
    echo.success("Region: %s" % r.context.region)
    echo.success("Port: 5432")
