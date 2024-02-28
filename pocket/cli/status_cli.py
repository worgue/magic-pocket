import click

from pocket.context import Context
from pocket.utils import echo


def get_resources(context: Context):
    resources = []
    if context.awscontainer:
        if context.awscontainer.vpc:
            resources.append(context.awscontainer.vpc.resource)
        resources.append(context.awscontainer.resource)
    if context.neon:
        resources.append(context.neon.resource)
    if context.s3:
        resources.append(context.s3.resource)
    return resources


@click.command()
@click.option("--stage", prompt=True)
def status(stage):
    context = Context.from_toml(stage=stage)
    resources = get_resources(context)
    for resource in resources:
        target_name = resource.__class__.__name__
        message = f"{target_name} status: {resource.status}"
        if resource.status == "NOEXIST":
            echo.info(message)
        elif resource.status == "REQUIRE_UPDATE":
            echo.warning(message)
        elif resource.status == "PROGRESS":
            echo.warning(message)
        elif resource.status == "COMPLETED":
            echo.success(message)
        elif resource.status == "FAILED":
            echo.danger(message)
        else:
            raise Exception(f"Unknown status: {resource.status}")
