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
        err = e.response["Error"]
        raise click.ClickException("%s: %s" % (err["Code"], err["Message"])) from e
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
