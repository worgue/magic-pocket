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


def show_status_message(resource):
    target_name = resource.__class__.__name__
    message = f"{target_name} status: {resource.status}"
    echo_fn = {
        "NOEXIST": echo.info,
        "REQUIRE_UPDATE": echo.warning,
        "PROGRESS": echo.warning,
        "COMPLETED": echo.success,
        "FAILED": echo.danger,
    }[resource.status]
    echo_fn(message)


def show_info_message(resource):
    print(resource.context.model_dump_json(indent=2))


@click.command()
@click.option("--show-info", is_flag=True, default=False)
@click.option("--stage", prompt=True)
def status(stage, show_info):
    context = Context.from_toml(stage=stage)
    resources = get_resources(context)
    for resource in resources:
        show_status_message(resource)
        if show_info:
            show_info_message(resource)
