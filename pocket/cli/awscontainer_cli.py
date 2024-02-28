import webbrowser

import click

from ..context import Context
from ..resources.awscontainer import AwsContainer
from ..utils import echo


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
@click.option("--stage", prompt=True)
def yaml(stage):
    ac = get_awscontainer_resource(stage)
    print(ac.stack.yaml)


@awscontainer.command()
@click.option("--stage", prompt=True)
def yaml_diff(stage):
    ac = get_awscontainer_resource(stage)
    print(ac.stack.yaml_diff.to_json(indent=2))


@awscontainer.command()
@click.option("--stage", prompt=True)
@click.option("--show-values", is_flag=True, default=False)
def secrets(stage, show_values):
    ac = get_awscontainer_resource(stage)
    if sm := ac.context.secretsmanager:
        for key, arn in sm.secrets.items():
            print("%s: %s" % (key, arn))
            if show_values:
                print("  - " + sm.resource.resolved_secrets[key])
    else:
        echo.warning("secretsmanager is not configured for this stage")


@awscontainer.command()
@click.option("--stage", prompt=True)
def create(stage):
    ac = get_awscontainer_resource(stage)
    if not ac.status == "NOEXIST":
        echo.warning("AWS lambda container is already created.")
    else:
        ac.create()
        echo.success("Created: lambda")


@awscontainer.command()
@click.option("--stage", prompt=True)
def update(stage):
    ac = get_awscontainer_resource(stage)
    if ac.status == "NOEXIST":
        echo.warning("AWS lambda has not created yet.")
        return
    if ac.status == "FAILED":
        echo.danger("AWS lambda has failed. Please check console.")
        return
    if ac.status == "PROGRESS":
        echo.warning("AWS lambda is updating. Please wait.")
        return
    ac.update()


@awscontainer.command()
@click.option("--stage", prompt=True)
def status(stage):
    ac = get_awscontainer_resource(stage)
    if ac.status == "COMPLETED":
        echo.success("Container is working!!!")
    elif ac.status == "NOEXIST":
        echo.warning("Container has not created yet.")
    elif ac.status == "FAILED":
        echo.danger("Container has failed. Please check console.")
    else:
        echo.warning("Container stack status: %s" % ac.stack.status)


@awscontainer.command()
@click.option("--stage", prompt=True)
@click.option("--open", is_flag=True, default=False)
def url(stage, open):
    ac = get_awscontainer_resource(stage)
    if ac.status == "COMPLETED":
        if endpoint := ac.endpoints.get("wsgi"):
            echo.success(f"wsgi url: {endpoint}")
            if open:
                webbrowser.open(endpoint)
        else:
            echo.warning("wsgi endpoint not found.")
    else:
        echo.warning("Container is not working.")
