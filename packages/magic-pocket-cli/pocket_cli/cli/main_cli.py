import click

from pocket import __version__
from pocket_cli import django_cli
from pocket_cli.cli import (
    awscontainer_cli,
    cloudfront_cli,
    cloudfront_keys_cli,
    deploy_cli,
    destroy_cli,
    dsql_cli,
    migrate_cli,
    neon_cli,
    rds_cli,
    runtime_config_cli,
    s3_cli,
    status_cli,
    tidb_cli,
    vpc_cli,
)


class PocketCLI(click.Group):
    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except ValueError as e:
            click.echo(f"エラー: {e}", err=True)
            ctx.exit(1)


@click.group(cls=PocketCLI)
def main():
    pass


@main.command()
def version():
    """Print the version number."""
    click.echo(__version__)


@main.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def context(stage):
    """Context を JSON で出力する（AWS API 呼び出しを伴う）。"""
    from pocket.context import Context

    ctx = Context.from_toml(stage=stage)
    print(ctx.model_dump_json(indent=2))


@main.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def settings(stage):
    """Settings を JSON で出力する（pocket.toml のみ、AWS 不要）。"""
    from pocket.settings import Settings

    s = Settings.from_toml(stage=stage)
    print(s.model_dump_json(indent=2))


main.add_command(deploy_cli.deploy)
main.add_command(destroy_cli.destroy)
main.add_command(status_cli.status)
main.add_command(django_cli.django)
main.add_command(runtime_config_cli.runtime_config)
main.add_command(migrate_cli.migrate)


@main.group()
def resource():
    pass


resource.add_command(vpc_cli.vpc)
resource.add_command(awscontainer_cli.awscontainer)
resource.add_command(neon_cli.neon)
resource.add_command(tidb_cli.tidb)
resource.add_command(dsql_cli.dsql)
resource.add_command(rds_cli.rds)
resource.add_command(s3_cli.s3)
resource.add_command(cloudfront_cli.cloudfront)
resource.add_command(cloudfront_keys_cli.cloudfront_keys)
