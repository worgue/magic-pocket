import click

from pocket.context import Context
from pocket.utils import echo


@click.command()
@click.option("--stage", prompt=True)
def deploy(stage):
    context = Context.from_toml(stage=stage)
    resources = []
    # if context.vpc:
    #     resources.append(context.vpc.resource)
    if context.awscontainer:
        resources.append(context.awscontainer.resource)
    if context.neon:
        resources.append(context.neon.resource)
    if context.s3:
        resources.append(context.s3.resource)
    for resource in resources:
        target_name = resource.__class__.__name__
        if resource.status == "NOEXIST":
            echo.log("Creating %s..." % target_name)
            resource.create()
        elif resource.status == "REQUIRE_UPDATE":
            echo.log("Updating %s..." % target_name)
            resource.update()
        else:
            echo.log("%s is already created." % target_name)
        # if target_name == "vpc":
        #     resource.wait_status("COMPLETED")
