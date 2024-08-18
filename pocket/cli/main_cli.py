import click

from .. import __version__
from ..django import django_cli
from . import (
    awscontainer_cli,
    deploy_cli,
    neon_cli,
    s3_cli,
    spa_cli,
    status_cli,
    vpc_cli,
)


@click.group()
def main():
    pass


@main.command()
def version():
    """Print the version number."""
    click.echo(__version__)


main.add_command(deploy_cli.deploy)
main.add_command(status_cli.status)
main.add_command(django_cli.django)


@main.group()
def resource():
    pass


resource.add_command(vpc_cli.vpc)
resource.add_command(awscontainer_cli.awscontainer)
resource.add_command(neon_cli.neon)
resource.add_command(s3_cli.s3)
resource.add_command(spa_cli.spa)
