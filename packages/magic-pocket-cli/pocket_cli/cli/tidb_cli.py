from pprint import pprint

import click

from pocket.context import Context
from pocket.utils import echo
from pocket_cli.cli.store_url_helper import run_store_url
from pocket_cli.resources.tidb import TiDb


@click.group()
def tidb():
    pass


def get_tidb_resource(stage):
    context = Context.from_toml(stage=stage)
    if not context.tidb:
        echo.danger("tidb is not configured for this stage")
        raise ValueError("tidb is not configured for this stage")
    return TiDb(context=context.tidb)


@tidb.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def context(stage):
    resource = get_tidb_resource(stage)
    pprint(resource.context.model_dump())


@tidb.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def create(stage):
    resource = get_tidb_resource(stage)
    resource.create()
    echo.success("TiDB cluster and database created")


@tidb.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def reset_database(stage):
    resource = get_tidb_resource(stage)
    resource.reset_database()
    echo.success("Reset database")


@tidb.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def delete(stage):
    resource = get_tidb_resource(stage)
    resource.delete_cluster()
    echo.success("Cluster was deleted successfully.")


@tidb.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option(
    "--key", default=None, help="保存先 user secret のキー (複数候補時に必須)"
)
@click.option("--force", is_flag=True, help="既存 secret があっても上書きする")
def store_url(stage, key, force):
    """cluster/db を ensure し DATABASE_URL を stored user secret に保存する。

    provisioning="command" で deploy を TiDB credential なしにするための provisioning
    ステップ。注意: TiDB serverless は password reveal API が無いため、本コマンドは
    実行のたびに root password をローテーションする (既存 secret は --force が必要。
    実行後は consumer の redeploy が前提)。
    """

    def ensure_and_compute_url(context):
        # TiDB は password reveal が無く ensure/url 算出で password を reset するため、
        # 同一インスタンスを使い回して password を整合させる (fresh instance にしない)。
        resource = TiDb(context.tidb)
        resource.create()
        return resource.database_url

    run_store_url(
        stage=stage,
        secret_type="tidb_database_url",  # noqa: S106 (secret type 名であって credential ではない)
        db_label="TiDB",
        key=key,
        force=force,
        ensure_and_compute_url=ensure_and_compute_url,
    )


@tidb.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def status(stage):
    resource = get_tidb_resource(stage)
    if resource.project:
        echo.success("Project found")
    else:
        echo.warning("Project not found")
        return
    if resource.cluster:
        echo.success(
            "Cluster found: %s (%s)" % (resource.cluster.name, resource.cluster.status)
        )
    else:
        echo.warning("Cluster not found")
        return
    if resource.cluster.status == "ACTIVE":
        echo.success("Database url: %s" % resource.database_url)
    else:
        echo.warning("Cluster status: %s" % resource.cluster.status)
