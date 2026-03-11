import click

from pocket.context import Context
from pocket.utils import echo
from pocket_cli.resources.dsql import Dsql


def _get_dsql_resource(stage: str) -> Dsql:
    context = Context.from_toml(stage=stage)
    if not context.dsql:
        raise click.ClickException("dsql is not configured for this stage")
    return Dsql(context.dsql)


@click.group()
def dsql():
    pass


@dsql.command()
@click.option("--stage", envvar="POCKET_STAGE", prompt=True)
def status(stage):
    """クラスター状態表示"""
    r = _get_dsql_resource(stage)
    echo.info("Tag Name: %s" % r.context.tag_name)
    echo.info("Status: %s" % r.status)
    if r.identifier:
        echo.info("Identifier: %s" % r.identifier)


@dsql.command()
@click.option("--stage", envvar="POCKET_STAGE", prompt=True)
def endpoint(stage):
    """接続情報表示"""
    r = _get_dsql_resource(stage)
    if not r.cluster:
        echo.warning("Cluster not found")
        return
    _print_endpoint(r)


@dsql.command()
@click.option("--stage", envvar="POCKET_STAGE", prompt=True)
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
