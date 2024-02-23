import click

from pocket import __version__
from pocket.cli import awscontainer_cli, deploy_cli, neon_cli, s3_cli, status_cli
from pocket.django import django_cli


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


resource.add_command(awscontainer_cli.awscontainer)
resource.add_command(neon_cli.neon)
resource.add_command(s3_cli.s3)
