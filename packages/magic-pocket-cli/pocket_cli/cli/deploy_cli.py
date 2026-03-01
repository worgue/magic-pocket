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
from pocket_cli.resources.rds import Rds
from pocket_cli.resources.s3 import S3
from pocket_cli.resources.tidb import TiDb
from pocket_cli.resources.vpc import Vpc


def _append_infra_resources(resources, context: Context, state_bucket: str):
    """VPC / RDS / CloudFrontKeys / AwsContainer をまとめて追加"""
    if context.awscontainer and context.awscontainer.vpc:
        if context.awscontainer.vpc.manage:
            resources.append(Vpc(context.awscontainer.vpc))
    if context.rds:
        resources.append(Rds(context.rds))
    for _name, cf_ctx in context.cloudfront.items():
        if cf_ctx.signing_key:
            resources.append(CloudFrontKeys(cf_ctx))
    if context.awscontainer:
        resources.append(
            AwsContainer(
                context.awscontainer,
                state_bucket=state_bucket,
                rds_context=context.rds,
            )
        )


def get_resources(context: Context, *, state_bucket: str = ""):
    resources = []
    if context.neon:
        resources.append(Neon(context.neon))
    if context.tidb:
        resources.append(TiDb(context.tidb))
    if context.s3:
        resources.append(S3(context.s3))
    _append_infra_resources(resources, context, state_bucket)
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


def deploy_init_resources(context: Context, *, state_bucket: str = ""):
    for resource in get_resources(context, state_bucket=state_bucket):
        target_name = resource.__class__.__name__
        echo.log("Deploy init %s..." % target_name)
        resource.deploy_init()


def deploy_frontend(context: Context, *, skip_build: bool = False):
    for _name, cf_ctx in context.cloudfront.items():
        cf = CloudFront(cf_ctx)
        if not cf_ctx.uploadable_routes:
            continue
        if cf.status == "NOEXIST":
            echo.warning("CloudFront '%s' が未作成です。スキップします。" % cf_ctx.name)
            continue
        cf.upload(skip_build=skip_build)


def deploy_resources(context: Context, *, state_bucket: str = ""):
    state_store = _create_state_store(context)
    # state bucket は deploy_init_resources の前に作成済み
    # ここでは念のため再確認
    state_store.ensure_bucket()

    mediator = Mediator(context)
    for resource in get_resources(context, state_bucket=state_bucket):
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
@click.option("--skip-frontend", is_flag=True, default=False)
def deploy(stage: str, openpath, skip_frontend):
    context = Context.from_toml(stage=stage)
    # CodeBuildがソースアップロードにstate bucketを必要とするため、先に作成
    state_store = _create_state_store(context)
    state_store.ensure_bucket()
    state_bucket = state_store.bucket_name
    deploy_init_resources(context, state_bucket=state_bucket)
    deploy_resources(context, state_bucket=state_bucket)
    if not skip_frontend:
        deploy_frontend(context)
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
