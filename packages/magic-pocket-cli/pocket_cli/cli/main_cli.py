import logging

import click

from pocket import __version__

try:
    from pocket_cli import django_cli
except ModuleNotFoundError as e:
    # 0.32.0 で django-storages が [django] extra へ移り、django は transitive に
    # 入らなくなった。django 未導入でも django 非依存のサブコマンドは使えるべき
    # なので、ここで落とさず `pocket django` だけを案内スタブに差し替える
    if (e.name or "").split(".")[0] != "django":
        raise
    django_cli = None

from pocket_cli.cli import (
    backup_cli,
    build_cli,
    cloudfront_cli,
    cloudfront_keys_cli,
    cloudfront_waf_cli,
    container_cli,
    deploy_cli,
    destroy_cli,
    dsql_cli,
    image_cli,
    migrate_cli,
    neon_cli,
    permissions_cli,
    rds_cli,
    runtime_config_cli,
    s3_cli,
    status_cli,
    tidb_cli,
    upstash_cli,
    vpc_cli,
    waf_cli,
)
from pocket_cli.cli.aws_auth import CREDENTIAL_EXCEPTIONS, print_credential_guide


def _django_unavailable_command() -> click.Command:
    """django 未導入時に `pocket django` へ登録する案内スタブ。

    生 traceback (ModuleNotFoundError: django) からは「何を install すれば
    直るのか」が読み手に伝わらないため、install 手順 1 行に変換して落とす。
    どのサブコマンド・引数でも同じ案内を出したいので、引数は全て素通しで
    受けて即エラーにする。
    """

    @click.command(
        name="django",
        context_settings={"ignore_unknown_options": True},
        add_help_option=False,
        short_help="Django 用コマンド (magic-pocket[django] が必要)",
    )
    @click.argument("args", nargs=-1, type=click.UNPROCESSED)
    def django(args):
        raise click.ClickException(
            "`pocket django` サブコマンドには django が必要です。\n"
            "magic-pocket を [django] extra 付きで入れ直してください:\n\n"
            '  uv tool install "magic-pocket-cli==%s"'
            ' --with "magic-pocket[django]==%s"\n\n'
            "(0.32.0 で django-storages が [django] extra へ移動したため、"
            "django は transitive に入らなくなりました)" % (__version__, __version__)
        )

    return django


class PocketCLI(click.Group):
    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except ValueError as e:
            click.echo(f"エラー: {e}", err=True)
            ctx.exit(1)
        except CREDENTIAL_EXCEPTIONS as e:
            # SSO 失効等で botocore の traceback が 100 行流れると「login し直せば
            # よい」という結論に辿り着けない。原因 1 行 + 認証手順に変換する
            print_credential_guide(e)
            ctx.exit(1)


@click.group(cls=PocketCLI)
def main():
    # SSO token の refresh 失敗時に botocore.tokens が WARNING + traceback を
    # 出すのを抑制する (失敗自体は上の CREDENTIAL_EXCEPTIONS 捕捉で案内される)
    logging.getLogger("botocore.tokens").setLevel(logging.ERROR)


@main.command()
def version():
    """Print the version number."""
    click.echo(__version__)


@main.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def context(stage):
    """Context を JSON で出力する（AWS API 呼び出しを伴う）。secret はマスク。"""
    import json

    from pocket.context import Context
    from pocket.utils import mask_secret_values

    ctx = Context.from_toml(stage=stage)
    print(json.dumps(mask_secret_values(ctx.model_dump(mode="json")), indent=2))


@main.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def settings(stage):
    """Settings を JSON で出力する（pocket.toml のみ、AWS 不要）。secret はマスク。"""
    import json

    from pocket.settings import Settings
    from pocket.utils import mask_secret_values

    s = Settings.from_toml(stage=stage)
    print(json.dumps(mask_secret_values(s.model_dump(mode="json")), indent=2))


main.add_command(build_cli.build)
main.add_command(deploy_cli.deploy)
main.add_command(deploy_cli.promote)
main.add_command(destroy_cli.destroy)
main.add_command(status_cli.status)
main.add_command(django_cli.django if django_cli else _django_unavailable_command())
main.add_command(runtime_config_cli.runtime_config)
main.add_command(migrate_cli.migrate)
main.add_command(permissions_cli.permissions)
main.add_command(waf_cli.waf)
main.add_command(backup_cli.backup)


@main.group()
def resource():
    pass


resource.add_command(vpc_cli.vpc)
resource.add_command(container_cli.container)
resource.add_command(neon_cli.neon)
resource.add_command(tidb_cli.tidb)
resource.add_command(upstash_cli.upstash)
resource.add_command(dsql_cli.dsql)
resource.add_command(rds_cli.rds)
resource.add_command(s3_cli.s3)
resource.add_command(cloudfront_cli.cloudfront)
resource.add_command(cloudfront_keys_cli.cloudfront_keys)
resource.add_command(cloudfront_waf_cli.cloudfront_waf)
resource.add_command(image_cli.image)
