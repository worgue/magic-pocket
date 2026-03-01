import click

from pocket.general_context import VpcContext
from pocket.utils import echo
from pocket_cli.resources.vpc import Vpc


@click.group()
def vpc():
    pass


def get_vpc_resource():
    vpc_context = VpcContext.from_toml()
    return Vpc(vpc_context)


@vpc.command()
def yaml():
    vpc = get_vpc_resource()
    print(vpc.stack.yaml)


@vpc.command()
def yaml_diff():
    vpc = get_vpc_resource()
    print(vpc.stack.yaml_diff.to_json(indent=2))


@vpc.command()
def create():
    vpc = get_vpc_resource()
    if not vpc.context.manage:
        echo.danger("外部 VPC は他で管理されています。")
        return
    if not vpc.status == "NOEXIST":
        echo.warning("AWS vpc is already created.")
    else:
        vpc.create()
        echo.success("Created: vpc")


@vpc.command()
def update():
    vpc = get_vpc_resource()
    if not vpc.context.manage:
        echo.danger("外部 VPC は他で管理されています。")
        return
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
def destroy():
    vpc = get_vpc_resource()
    if not vpc.context.manage:
        echo.danger("外部 VPC は他で管理されています。")
        return
    if vpc.stack.consumers:
        echo.danger("VPC に consumer がいるため削除できません:")
        for c in vpc.stack.consumers:
            echo.info("  - %s" % c)
        return
    has_stack = vpc.stack.status != "NOEXIST"
    has_efs = vpc.efs and vpc.efs.exists()
    if not has_stack and not has_efs:
        echo.warning("No VPC resources found.")
        return
    click.confirm("VPC を削除しますか？", abort=True)
    vpc.delete()
    echo.success("VPC was destroyed.")


@vpc.command()
def status():
    vpc = get_vpc_resource()
    if vpc.status == "COMPLETED":
        echo.success("Vpc has been created.")
    elif vpc.status == "NOEXIST":
        echo.warning("Vpc has not created yet.")
    elif vpc.status == "FAILED":
        echo.danger("Vpc has failed. Please check console.")
    else:
        echo.warning("Vpc stack status: %s" % vpc.stack.status)
