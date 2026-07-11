import json

import click

from pocket.context import Context
from pocket.utils import echo
from pocket_cli.resources.rds import Rds


def _get_rds_resource(stage: str) -> Rds:
    context = Context.from_toml(stage=stage)
    if not context.rds:
        raise click.ClickException("rds is not configured for this stage")
    return Rds(context.rds)


@click.group()
def rds():
    pass


@rds.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def status(stage):
    """クラスター状態表示"""
    r = _get_rds_resource(stage)
    echo.info("Cluster: %s" % r.context.cluster_identifier)
    echo.info("Status: %s" % r.status)
    if r.cluster:
        echo.info("Engine: %s" % r.cluster.get("EngineVersion", ""))


@rds.command()
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
    r = _get_rds_resource(stage)
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
                    "endpoint": r.cluster.get("Endpoint", ""),
                    "port": r.cluster.get("Port", ""),
                    "database": r.context.database_name,
                    "username": r.context.master_username,
                }
            )
        )
        return
    _print_endpoint(r)


@rds.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def destroy(stage):
    """確認付き削除"""
    r = _get_rds_resource(stage)
    if r.status == "NOEXIST":
        echo.info("RDS cluster does not exist.")
        return
    click.confirm(
        "RDS Aurora クラスター '%s' を削除しますか？" % r.context.cluster_identifier,
        abort=True,
    )
    r.delete()
    echo.success("RDS Aurora cluster was destroyed. Final snapshot was created.")


def _print_endpoint(r: Rds):
    if not r.cluster:
        raise RuntimeError("RDS cluster is not available")
    echo.success("Endpoint: %s" % r.cluster.get("Endpoint", ""))
    echo.success("Port: %s" % r.cluster.get("Port", ""))
    echo.success("Database: %s" % r.context.database_name)
    echo.success("Username: %s" % r.context.master_username)
