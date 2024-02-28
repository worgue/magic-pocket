from pprint import pprint

import click

from ..context import VpcContext
from ..utils import echo


@click.group()
def vpc():
    pass


def get_vpc_resource(ref):
    vpc_context = VpcContext.from_toml(ref=ref)
    return vpc_context.resource


@vpc.command()
@click.option("--ref", prompt=True)
def yaml(ref):
    vpc = get_vpc_resource(ref)
    print(vpc.stack.yaml)


@vpc.command()
@click.option("--ref", prompt=True)
def yaml_diff(ref):
    vpc = get_vpc_resource(ref)
    pprint(vpc.stack.yaml_diff)


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
