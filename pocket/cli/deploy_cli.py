import click

from ..context import Context
from ..utils import echo


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
def deploy(stage):
    context = Context.from_toml(stage=stage)
    resources = get_resources(context)
    for resource in resources:
        target_name = resource.__class__.__name__
        echo.log("Deploy init %s..." % target_name)
        resource.deploy_init()
        if resource.status == "NOEXIST":
            echo.log("Creating %s..." % target_name)
            resource.create()
        elif resource.status == "REQUIRE_UPDATE":
            echo.log("Updating %s..." % target_name)
            resource.update()
        else:
            echo.log("%s is already created." % target_name)
