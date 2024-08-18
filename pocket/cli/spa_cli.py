import click

from ..context import Context
from ..resources.spa import Spa
from ..utils import echo


@click.group()
def spa():
    pass


def get_spa_resource(stage):
    context = Context.from_toml(stage=stage)
    if not context.spa:
        echo.danger("spa is not configured for this stage")
        raise Exception("spa is not configured for this stage")
    return Spa(context=context.spa)


@spa.command()
@click.option("--stage", prompt=True)
def context(stage):
    spa = get_spa_resource(stage)
    print(spa.context.model_dump_json(indent=2))


@spa.command()
@click.option("--stage", prompt=True)
def create(stage):
    spa = get_spa_resource(stage)
    spa.create()
    echo.success("spa store was created")


@spa.command()
@click.option("--stage", prompt=True)
def delete(stage):
    raise NotImplementedError("delete method is not implemented yet")
    spa = get_spa_resource(stage)
    spa.delete()
    echo.success("spa store was deleted successfully.")


@spa.command()
@click.option("--stage", prompt=True)
def status(stage):
    spa = get_spa_resource(stage)
    if spa._s3_exists():
        echo.success("s3 for spa exists")
    if spa.status == "COMPLETED":
        echo.success("completed")
    print(spa.status)
