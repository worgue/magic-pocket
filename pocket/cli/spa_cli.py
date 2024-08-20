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
def yaml(stage):
    spa = get_spa_resource(stage)
    print(spa.stack.yaml)


@spa.command()
@click.option("--stage", prompt=True)
def yaml_diff(stage):
    spa = get_spa_resource(stage)
    print(spa.stack.yaml_diff.to_json(indent=2))


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
def destroy(stage):
    spa = get_spa_resource(stage)
    spa.delete()
    echo.success("spa store was deleted successfully.")


@spa.command()
@click.option("--stage", prompt=True)
def update(stage):
    spa = get_spa_resource(stage)
    if spa.status == "NOEXIST":
        echo.warning("Spa resource has not created yet.")
        return
    if spa.status == "FAILED":
        echo.danger("Spa resource creation has failed. Please check console.")
        return
    if spa.status == "PROGRESS":
        echo.warning("Spa is updating. Please wait.")
        return
    spa.update()


@spa.command()
@click.option("--stage", prompt=True)
def status(stage):
    spa = get_spa_resource(stage)
    if spa._s3_exists():
        echo.info("s3 for spa exists")
    if spa.status == "COMPLETED":
        echo.success("COMPLETED")
    else:
        print(spa.status)
