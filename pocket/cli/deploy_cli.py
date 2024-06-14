import inspect

import click

from ..context import Context
from ..mediator import Mediator
from ..utils import echo


def get_resources(context: Context):
    resources = []
    if context.neon:
        resources.append(context.neon.resource)
    if context.s3:
        resources.append(context.s3.resource)
    if context.awscontainer:
        if context.awscontainer.vpc:
            resources.append(context.awscontainer.vpc.resource)
        resources.append(context.awscontainer.resource)
    return resources


def _deploy_init_resources(context: Context):
    for resource in get_resources(context):
        target_name = resource.__class__.__name__
        echo.log("Deploy init %s..." % target_name)
        resource.deploy_init()


def _deploy_resources(context: Context):
    mediator = Mediator(context)
    for resource in get_resources(context):
        target_name = resource.__class__.__name__
        if resource.status == "NOEXIST":
            echo.log("Creating %s..." % target_name)
            if "mediator" in inspect.signature(resource.create).parameters:
                resource.create(mediator)
            else:
                resource.create()
        elif resource.status == "REQUIRE_UPDATE":
            echo.log("Updating %s..." % target_name)
            if "mediator" in inspect.signature(resource.update).parameters:
                resource.update(mediator)
            else:
                resource.update()
        else:
            echo.log("%s is already created." % target_name)


@click.command()
@click.option("--stage", prompt=True)
def deploy(stage):
    context = Context.from_toml(stage=stage)
    _deploy_init_resources(context)
    _deploy_resources(context)
