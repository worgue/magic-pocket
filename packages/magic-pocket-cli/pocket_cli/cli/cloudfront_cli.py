import click

from pocket.context import Context
from pocket.utils import echo
from pocket_cli.resources.cloudfront import CloudFront


@click.group()
def cloudfront():
    pass


def get_cloudfront_resources(stage, name=None):
    context = Context.from_toml(stage=stage)
    if not context.cloudfront:
        echo.danger("cloudfront is not configured for this stage")
        raise Exception("cloudfront is not configured for this stage")
    if name:
        if name not in context.cloudfront:
            echo.danger("cloudfront '%s' is not configured" % name)
            raise Exception("cloudfront '%s' is not configured" % name)
        return [CloudFront(context.cloudfront[name])]
    return [CloudFront(cf_ctx) for cf_ctx in context.cloudfront.values()]


@cloudfront.command()
@click.option("--stage", envvar="POCKET_STAGE", prompt=True)
@click.option("--name", default=None)
def yaml(stage, name):
    for cf in get_cloudfront_resources(stage, name):
        echo.info("[%s]" % cf.context.name)
        print(cf.stack.yaml)


@cloudfront.command()
@click.option("--stage", envvar="POCKET_STAGE", prompt=True)
@click.option("--name", default=None)
def yaml_diff(stage, name):
    for cf in get_cloudfront_resources(stage, name):
        echo.info("[%s]" % cf.context.name)
        print(cf.stack.yaml_diff.to_json(indent=2))


@cloudfront.command()
@click.option("--stage", envvar="POCKET_STAGE", prompt=True)
@click.option("--name", default=None)
def context(stage, name):
    for cf in get_cloudfront_resources(stage, name):
        echo.info("[%s]" % cf.context.name)
        print(cf.context.model_dump_json(indent=2))


@cloudfront.command()
@click.option("--stage", envvar="POCKET_STAGE", prompt=True)
@click.option("--name", default=None)
def create(stage, name):
    for cf in get_cloudfront_resources(stage, name):
        echo.info("[%s]" % cf.context.name)
        cf.create()
    echo.success("cloudfront store was created")


@cloudfront.command()
@click.option("--stage", envvar="POCKET_STAGE", prompt=True)
@click.option("--name", default=None)
def destroy(stage, name):
    for cf in get_cloudfront_resources(stage, name):
        echo.info("[%s]" % cf.context.name)
        cf.delete()
    echo.success("cloudfront store was deleted successfully.")


@cloudfront.command()
@click.option("--stage", envvar="POCKET_STAGE", prompt=True)
@click.option("--name", default=None)
def update(stage, name):
    for cf in get_cloudfront_resources(stage, name):
        echo.info("[%s]" % cf.context.name)
        if cf.status == "NOEXIST":
            echo.warning("CloudFront resource has not created yet.")
            continue
        if cf.status == "FAILED":
            echo.danger(
                "CloudFront resource creation has failed. Please check console."
            )
            continue
        if cf.status == "PROGRESS":
            echo.warning("CloudFront is updating. Please wait.")
            continue
        cf.update()


@cloudfront.command()
@click.option("--stage", envvar="POCKET_STAGE", prompt=True)
@click.option("--name", default=None)
def status(stage, name):
    for cf in get_cloudfront_resources(stage, name):
        echo.info("[%s]" % cf.context.name)
        if cf.status == "COMPLETED":
            echo.success("COMPLETED")
        else:
            print(cf.status)
