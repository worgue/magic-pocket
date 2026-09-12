import click

from pocket.general_context import VpcContext
from pocket.settings import Settings
from pocket.utils import echo
from pocket_cli.resources.vpc import Vpc


@click.group()
def vpc():
    pass


def get_vpc_resource(stage: str | None = None):
    if stage is None:
        vpc_context = VpcContext.from_toml()
    else:
        settings = Settings.from_toml(stage=stage)
        if settings.vpc is None:
            raise click.ClickException(
                f"stage '{stage}' に VPC が定義されていません。"
                "撤去する場合は元の VPC 宣言を復元してください。"
            )
        vpc_context = VpcContext.from_settings(settings.vpc, settings.general)
    return Vpc(vpc_context)


@vpc.command()
@click.option(
    "--stage",
    envvar="POCKET_DEPLOY_STAGE",
    help="対象ステージ。省略時は共通の [vpc] を使用",
)
def yaml(stage: str | None):
    vpc = get_vpc_resource(stage)
    print(vpc.stack.yaml)


@vpc.command()
@click.option(
    "--stage",
    envvar="POCKET_DEPLOY_STAGE",
    help="対象ステージ。省略時は共通の [vpc] を使用",
)
def yaml_diff(stage: str | None):
    vpc = get_vpc_resource(stage)
    print(vpc.stack.yaml_diff.to_json(indent=2))


@vpc.command()
@click.option(
    "--stage",
    envvar="POCKET_DEPLOY_STAGE",
    help="対象ステージ。省略時は共通の [vpc] を使用",
)
def create(stage: str | None):
    vpc = get_vpc_resource(stage)
    if not vpc.context.manage:
        echo.danger("外部 VPC は他で管理されています。")
        return
    if not vpc.status == "NOEXIST":
        echo.warning("AWS vpc is already created.")
    else:
        vpc.create()
        echo.success("Created: vpc")


@vpc.command()
@click.option(
    "--stage",
    envvar="POCKET_DEPLOY_STAGE",
    help="対象ステージ。省略時は共通の [vpc] を使用",
)
def update(stage: str | None):
    vpc = get_vpc_resource(stage)
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
@click.option(
    "--stage",
    envvar="POCKET_DEPLOY_STAGE",
    help="対象ステージ。省略時は共通の [vpc] を使用",
)
def destroy(stage: str | None):
    vpc = get_vpc_resource(stage)
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
@click.option(
    "--stage",
    envvar="POCKET_DEPLOY_STAGE",
    help="対象ステージ。省略時は共通の [vpc] を使用",
)
def status(stage: str | None):
    vpc = get_vpc_resource(stage)
    if vpc.status == "COMPLETED":
        echo.success("Vpc has been created.")
    elif vpc.status == "NOEXIST":
        echo.warning("Vpc has not created yet.")
    elif vpc.status == "FAILED":
        echo.danger("Vpc has failed. Please check console.")
    else:
        echo.warning("Vpc stack status: %s" % vpc.stack.status)
