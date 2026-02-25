from pprint import pprint

import click

from pocket.context import Context
from pocket.utils import echo
from pocket_cli.resources.neon import Neon


@click.group()
def neon():
    pass


def get_neon_resource(stage):
    context = Context.from_toml(stage=stage)
    if not context.neon:
        echo.danger("neon is not configured for this stage")
        raise Exception("neon is not configured for this stage")
    return Neon(context=context.neon)


@neon.command()
@click.option("--stage", envvar="POCKET_STAGE", prompt=True)
def context(stage):
    neon = get_neon_resource(stage)
    pprint(neon.context.model_dump())


@neon.command()
@click.option("--stage", envvar="POCKET_STAGE", prompt=True)
def create(stage):
    neon = get_neon_resource(stage)
    neon.create()
    echo.success("New branch was created")


@neon.command()
@click.option("--stage", envvar="POCKET_STAGE", prompt=True)
def reset_database(stage):
    neon = get_neon_resource(stage)
    neon.reset_database()
    echo.success("Reset database")


@neon.command()
@click.option("--stage", envvar="POCKET_STAGE", prompt=True)
@click.option("--base-stage", default=None)
def branch_out(stage, base_stage):
    neon = get_neon_resource(stage)
    if neon.branch:
        raise Exception("Branch already exists")
    base_neon = get_neon_resource(base_stage)
    if not base_neon.working:
        raise Exception("Base stage is not working")
    assert base_neon.branch
    neon.create_branch(base_neon.branch)
    echo.success("New branch was created")


@neon.command()
@click.option("--stage", envvar="POCKET_STAGE", prompt=True)
def delete(stage):
    neon = get_neon_resource(stage)
    neon.delete_branch()
    echo.success("Branch was deleted successfully.")


@neon.command()
@click.option("--stage", envvar="POCKET_STAGE", prompt=True)
def status(stage):
    neon = get_neon_resource(stage)
    if neon.project:
        echo.success("Project found")
    else:
        echo.warning("Project not found")
        return
    if neon.branch:
        echo.success("Branch found")
    else:
        echo.warning("Branch not found")
        return
    if neon.database:
        echo.success("Database found")
    else:
        echo.warning("Database not found")
        return
    if neon.endpoint:
        echo.success("Endpoint found: %s" % neon.endpoint.host)
    else:
        echo.warning("Endpoint not found")
    if neon.role:
        echo.success("Role found: %s" % neon.context.role_name)
    else:
        echo.warning("Role not found")
    if neon.role and neon.endpoint:
        echo.success("Database url: %s" % neon.database_url)
