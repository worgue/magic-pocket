import click

from ..context import Context
from ..utils import echo


@click.group()
def cloudfront():
    pass


def get_cloudfront_resource(stage):
    context = Context.from_toml(stage=stage)
    if not context.cloudfront:
        echo.danger("cloudfront is not configured for this stage")
        raise Exception("cloudfront is not configured for this stage")
    return context.cloudfront.resource


@cloudfront.command()
@click.option("--stage", prompt=True)
def yaml(stage):
    cloudfront = get_cloudfront_resource(stage)
    print(cloudfront.stack.yaml)


@cloudfront.command()
@click.option("--stage", prompt=True)
def yaml_diff(stage):
    cloudfront = get_cloudfront_resource(stage)
    print(cloudfront.stack.yaml_diff.to_json(indent=2))


@cloudfront.command()
@click.option("--stage", prompt=True)
def context(stage):
    cloudfront = get_cloudfront_resource(stage)
    print(cloudfront.context.model_dump_json(indent=2))


@cloudfront.command()
@click.option("--stage", prompt=True)
def create(stage):
    cloudfront = get_cloudfront_resource(stage)
    cloudfront.create()
    echo.success("cloudfront store was created")


@cloudfront.command()
@click.option("--stage", prompt=True)
def destroy(stage):
    cloudfront = get_cloudfront_resource(stage)
    cloudfront.delete()
    echo.success("cloudfront store was deleted successfully.")


@cloudfront.command()
@click.option("--stage", prompt=True)
def update(stage):
    cloudfront = get_cloudfront_resource(stage)
    if cloudfront.status == "NOEXIST":
        echo.warning("CloudFront resource has not created yet.")
        return
    if cloudfront.status == "FAILED":
        echo.danger("CloudFront resource creation has failed. Please check console.")
        return
    if cloudfront.status == "PROGRESS":
        echo.warning("CloufFront is updating. Please wait.")
        return
    cloudfront.update()


@cloudfront.command()
@click.option("--stage", prompt=True)
def status(stage):
    cloudfront = get_cloudfront_resource(stage)
    if cloudfront._origin_s3_exists():
        echo.info("s3 for cloudfront exists")
    if cloudfront.status == "COMPLETED":
        echo.success("COMPLETED")
    else:
        print(cloudfront.status)
