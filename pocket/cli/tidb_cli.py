from pprint import pprint

import click

from ..context import Context
from ..resources.tidb import TiDb
from ..utils import echo


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
@click.option("--stage", prompt=True)
def context(stage):
    resource = get_tidb_resource(stage)
    pprint(resource.context.model_dump())


@tidb.command()
@click.option("--stage", prompt=True)
def create(stage):
    resource = get_tidb_resource(stage)
    resource.create()
    echo.success("TiDB cluster and database created")


@tidb.command()
@click.option("--stage", prompt=True)
def reset_database(stage):
    resource = get_tidb_resource(stage)
    resource.reset_database()
    echo.success("Reset database")


@tidb.command()
@click.option("--stage", prompt=True)
def delete(stage):
    resource = get_tidb_resource(stage)
    resource.delete_cluster()
    echo.success("Cluster was deleted successfully.")


@tidb.command()
@click.option("--stage", prompt=True)
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
