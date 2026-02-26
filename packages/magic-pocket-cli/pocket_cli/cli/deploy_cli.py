import inspect
import webbrowser

import click

from pocket.context import Context
from pocket.utils import echo
from pocket_cli.mediator import Mediator
from pocket_cli.resources.aws.state import StateStore
from pocket_cli.resources.awscontainer import AwsContainer
from pocket_cli.resources.cloudfront import CloudFront
from pocket_cli.resources.cloudfront_keys import CloudFrontKeys
from pocket_cli.resources.neon import Neon
from pocket_cli.resources.s3 import S3
from pocket_cli.resources.tidb import TiDb
from pocket_cli.resources.vpc import Vpc


def get_resources(context: Context):
    resources = []
    if context.neon:
        resources.append(Neon(context.neon))
    if context.tidb:
        resources.append(TiDb(context.tidb))
    if context.s3:
        resources.append(S3(context.s3))
    if context.awscontainer:
        if context.awscontainer.vpc:
            resources.append(Vpc(context.awscontainer.vpc))
    # CloudFrontKeys は AwsContainer より前（PublicKeyId Export が必要）
    for _name, cf_ctx in context.cloudfront.items():
        if cf_ctx.signing_key:
            resources.append(CloudFrontKeys(cf_ctx))
    if context.awscontainer:
        resources.append(AwsContainer(context.awscontainer))
    for _name, cf_ctx in context.cloudfront.items():
        resources.append(CloudFront(cf_ctx))
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
    if endpoint := context.awscontainer and AwsContainer(
        context.awscontainer
    ).endpoints.get("wsgi"):
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
