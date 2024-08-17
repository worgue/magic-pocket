import json
import warnings
import webbrowser
from subprocess import run

import click

from ..cli.deploy_cli import deploy_init_resources, deploy_resources
from ..context import Context
from ..utils import echo
from . import django_installed
from .utils import get_storages


@click.group()
def django():
    pass


@django.command()
@click.option("--stage", prompt=True)
@click.option("--openpath")
@click.option("--force", is_flag=True, default=False)
def deploy(stage: str, openpath, force):
    context = Context.from_toml(stage=stage)
    deploy_init_resources(context)
    deploy_resources(context)
    handler = _get_management_command_handler(context)
    if force or click.confirm("collectstatic?"):
        res = handler.invoke(
            json.dumps({"command": "collectstatic", "args": ["--noinput"]})
        )
        handler.show_logs(res)
    if force or click.confirm("migrate?"):
        res = handler.invoke(json.dumps({"command": "migrate", "args": []}))
        handler.show_logs(res)
    if endpoint := context.awscontainer and context.awscontainer.resource.endpoints.get(
        "wsgi"
    ):
        echo.success(f"wsgi url: {endpoint}")
        if openpath:
            webbrowser.open(endpoint + "/" + openpath)


def _get_management_command_handler(context: Context, key: str | None = None):
    if not context.awscontainer:
        raise Exception("awscontainer is not configured for this stage")
    if key:
        warnings.warn(
            "Do not use key to get management command handler",
            DeprecationWarning,
            stacklevel=2,
        )
        return context.awscontainer.resource.handlers[key]
    target_command = "pocket.django.lambda_handlers.management_command_handler"
    for key, handler_context in context.awscontainer.handlers.items():
        if handler_context.command == target_command:
            return context.awscontainer.resource.handlers[key]
    print("management command handler not found")
    raise Exception("Add management command handler for this stage")


@django.command(
    context_settings={
        "ignore_unknown_options": True,
    },
)
@click.option("--stage", prompt=True)
@click.argument("command")
@click.argument("args", nargs=-1)
@click.option("--handler")
def manage(stage, command, args, handler):
    if not django_installed:
        raise Exception("django is not installed")
    context = Context.from_toml(stage=stage)
    handler = _get_management_command_handler(context, key=handler)
    res = handler.invoke(json.dumps({"command": command, "args": args}))
    handler.show_logs(res)


@django.group()
def storage():
    pass


def _check_upload_backends(from_storage, to_storage):
    if from_storage["BACKEND"] != "django.core.files.storage.FileSystemStorage":
        raise Exception("Upload from only support FileSystemStorage")
    if to_storage["BACKEND"] != "storages.backends.s3boto3.S3Boto3Storage":
        raise Exception("Upload to only support S3Boto3Storage")


@storage.command()
@click.option("--stage", prompt=True)
@click.option("--delete", is_flag=True, default=False)
@click.argument("storage")
def upload(storage, stage, delete):
    from_storage = get_storages()[storage]
    to_storage = get_storages(stage=stage)[storage]
    _check_upload_backends(from_storage, to_storage)
    from_location = from_storage["OPTIONS"]["location"]
    to_backet_name = to_storage["OPTIONS"]["bucket_name"]
    to_location = to_storage["OPTIONS"]["location"]
    cmd = "aws s3 sync %s s3://%s/%s" % (from_location, to_backet_name, to_location)
    cmd += ' --exclude ".*" --exclude "*/.*"'
    if delete:
        cmd += " --delete"
    print(cmd)
    run(cmd, shell=True, check=True)
