import json

import click
from botocore.exceptions import ClientError

from pocket.context import Context
from pocket.utils import echo
from pocket_cli.cli.resource_helper import require_configured
from pocket_cli.resources.dsql import (
    BACKUP_ROLE_NAME,
    BACKUP_TERMINAL_STATES,
    BACKUP_VAULT_NAME,
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
    " (省略時: pocket 管理の '%s' を自動作成して使用)" % BACKUP_VAULT_NAME,
)
@click.option(
    "--iam-role-arn",
    default=None,
    help="AWS Backup が assume するロール ARN"
    " (省略時: pocket 管理の '%s' を自動作成して使用)" % BACKUP_ROLE_NAME,
)
@click.option(
    "--retention-days",
    type=int,
    default=None,
    help="recovery point の保持日数 (省略時: vault のライフサイクル既定)",
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
def restore(stage, recovery_point_arn, latest, yes, skip_backup):
    """バックアップから復元し、現用クラスターを切り替える"""
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
    _finish_restore(r, stage, job_id)


@dsql.command("restore-status")
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--job-id", required=True, help="restore job ID")
def restore_status(stage, job_id):
    """復元 job の状態確認（完了していれば切り替えを完了させる）"""
    r = _get_dsql_resource(stage)
    job = r.get_restore_job(job_id)
    echo.info("Job ID: %s" % job_id)
    echo.info("Status: %s" % job["Status"])
    if job.get("StatusMessage"):
        echo.info("Message: %s" % job["StatusMessage"])
    _finish_restore(r, stage, job_id)


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


def _finish_restore(r: Dsql, stage: str, job_id: str):
    """restore job の完了を待ち、現用クラスターの切り替えまで行う。

    切り替え (Name タグの付け替えと endpoint の publish) は冪等なので、待機が
    中断された後に restore-status から再実行しても安全。
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
