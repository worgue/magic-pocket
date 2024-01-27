from pprint import pprint

import click

from pocket import __version__
from pocket.context import Context
from pocket.resources.awscontainer import AwsContainer


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


@resource.command()
@click.argument("action")
@click.option("--stage", prompt=True)
def awscontainer(action, stage):
    context = Context.from_toml(stage=stage)
    if action == "yaml":
        ac = AwsContainer(context)
        pprint(ac.stack.yaml)
