from pprint import pprint

import click

from pocket.context import Context
from pocket.resources.awscontainer import AwsContainer
from pocket.utils import echo


@click.group()
def awscontainer():
    pass


def get_awscontainer_resource(stage):
    context = Context.from_toml(stage=stage)
    if not context.awscontainer:
        echo.danger("awscontainer is not configured for this stage")
        raise Exception("awscontainer is not configured for this stage")
    return AwsContainer(context=context.awscontainer)


@awscontainer.command()
@click.argument("action")
@click.option("--stage", prompt=True)
def yaml(action, stage):
    ac = get_awscontainer_resource(stage)
    pprint(ac.stack.yaml)
