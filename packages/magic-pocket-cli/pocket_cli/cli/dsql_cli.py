import json

import click

from pocket.context import Context
from pocket.utils import echo
from pocket_cli.cli.resource_helper import require_configured
from pocket_cli.resources.dsql import Dsql


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
