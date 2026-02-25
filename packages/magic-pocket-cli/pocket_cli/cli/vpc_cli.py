import click

from pocket.general_context import VpcContext
from pocket.utils import echo
from pocket_cli.resources.vpc import Vpc


@click.group()
def vpc():
    pass


def get_vpc_resource(ref):
    vpc_context = VpcContext.from_toml(ref=ref)
    return Vpc(vpc_context)


@vpc.command()
@click.option("--ref", prompt=True)
def yaml(ref):
    vpc = get_vpc_resource(ref)
    print(vpc.stack.yaml)


@vpc.command()
@click.option("--ref", prompt=True)
def yaml_diff(ref):
    vpc = get_vpc_resource(ref)
    print(vpc.stack.yaml_diff.to_json(indent=2))


@vpc.command()
@click.option("--ref", prompt=True)
def create(ref):
    vpc = get_vpc_resource(ref)
    if not vpc.status == "NOEXIST":
        echo.warning("AWS vpc is already created.")
    else:
        vpc.create()
        echo.success("Created: vpc")


@vpc.command()
@click.option("--ref", prompt=True)
def update(ref):
    vpc = get_vpc_resource(ref)
    if vpc.status == "NOEXIST":
        echo.warning("vpc has not created yet.")
        return
    if vpc.status == "FAILED":
        echo.danger("vpc has failed. Please check console.")
        return
    if vpc.status == "PROGRESS":
        echo.warning("vpc is updating. Please wait.")
        return
    vpc.update()


@vpc.command()
@click.option("--ref", prompt=True)
def destroy(ref):
    vpc = get_vpc_resource(ref)
    has_stack = vpc.stack.status != "NOEXIST"
    has_efs = vpc.efs and vpc.efs.exists()
    if not has_stack and not has_efs:
        echo.warning("No VPC resources found.")
        return
    click.confirm("VPC を削除しますか？", abort=True)
    vpc.delete()
    echo.success("VPC was destroyed.")


@vpc.command()
@click.option("--ref", prompt=True)
def status(reg):
    vpc = get_vpc_resource(reg)
    if vpc.status == "COMPLETED":
        echo.success("Vpc has been created.")
    elif vpc.status == "NOEXIST":
        echo.warning("Vpc has not created yet.")
    elif vpc.status == "FAILED":
        echo.danger("Vpc has failed. Please check console.")
    else:
        echo.warning("Vpc stack status: %s" % vpc.stack.status)
