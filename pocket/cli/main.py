import click

from pocket import __version__
from pocket.cli import awscontainer_cli, neon_cli


@click.group()
def main():
    pass


@main.command()
def version():
    """Print the version number."""
    click.echo(__version__)


@main.group()
def resource():
    pass


resource.add_command(awscontainer_cli.awscontainer)
resource.add_command(neon_cli.neon)
