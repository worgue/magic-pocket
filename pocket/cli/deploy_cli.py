import inspect
import webbrowser

import click

from ..context import Context
from ..mediator import Mediator
from ..resources.aws.state import StateStore
from ..utils import echo


def get_resources(context: Context):
    resources = []
    if context.neon:
        resources.append(context.neon.resource)
    if context.tidb:
        resources.append(context.tidb.resource)
    if context.s3:
        resources.append(context.s3.resource)
    if context.awscontainer:
        if context.awscontainer.vpc:
            resources.append(context.awscontainer.vpc.resource)
        resources.append(context.awscontainer.resource)
    if context.cloudfront:
        resources.append(context.cloudfront.resource)
    return resources


def _create_state_store(context: Context) -> StateStore:
    assert context.general
    resource_prefix = context.general.prefix_template.format(
        stage=context.stage,
        project=context.project_name,
        namespace=context.general.namespace,
    )
    bucket_name = f"{resource_prefix}state"
    return StateStore(bucket_name, context.general.region)


def deploy_init_resources(context: Context):
    for resource in get_resources(context):
        target_name = resource.__class__.__name__
        echo.log("Deploy init %s..." % target_name)
        resource.deploy_init()


def deploy_resources(context: Context):
    state_store = _create_state_store(context)
    state_store.ensure_bucket()

    mediator = Mediator(context)
    for resource in get_resources(context):
        target_name = resource.__class__.__name__
        if resource.status == "NOEXIST":
            echo.log("Creating %s..." % target_name)
            if "mediator" in inspect.signature(resource.create).parameters:
                resource.create(mediator)
            else:
                resource.create()
            state_store.record(resource.state_info())
        elif resource.status == "REQUIRE_UPDATE":
            echo.log("Updating %s..." % target_name)
            if "mediator" in inspect.signature(resource.update).parameters:
                resource.update(mediator)
            else:
                resource.update()
            state_store.record(resource.state_info())
        else:
            echo.log("%s is already the latest version." % target_name)


@click.command()
@click.option("--stage", envvar="POCKET_STAGE", prompt=True)
@click.option("--openpath")
def deploy(stage: str, openpath):
    context = Context.from_toml(stage=stage)
    deploy_init_resources(context)
    deploy_resources(context)
    if endpoint := context.awscontainer and context.awscontainer.resource.endpoints.get(
        "wsgi"
    ):
        if endpoint is None:
            echo.warning("wsgi endpoint is not created yet.")
            echo.warning("You can check the endpoint later by resource command.")
            echo.warning("$ pocket resource awscontainer url")
            echo.warning("Or deploy again.")
            echo.warning("$ pocket deploy")
        else:
            echo.success(f"wsgi url: {endpoint}")
            if openpath:
                webbrowser.open(endpoint + "/" + openpath)
