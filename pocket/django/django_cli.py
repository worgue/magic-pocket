import json
from email.utils import parsedate_to_datetime
from subprocess import run

import click

from ..context import Context
from . import django_installed
from .utils import get_storages


@click.group()
def django():
    pass


@django.command(
    context_settings={
        "ignore_unknown_options": True,
    },
)
@click.option("--stage", prompt=True)
@click.option("--handler", prompt=True)
@click.argument("command")
@click.argument("args", nargs=-1)
def manage(stage, handler, command, args):
    if not django_installed:
        raise Exception("django is not installed")
    context = Context.from_toml(stage=stage)
    if not context.awscontainer:
        raise Exception("awscontainer is not configured for this stage")
    handler_context = context.awscontainer.handlers.get(handler)
    handler = context.awscontainer.resource.handlers.get(handler)
    if handler_context is None or handler is None:
        raise Exception("handler %s is not configured for this stage" % handler_context)
    if (
        handler_context.command
        != "pocket.django.lambda_handlers.management_command_handler"
    ):
        raise Exception("handler %s is not management handler" % handler_context)
    payload = json.dumps({"command": command, "args": args})
    res = handler.invoke(payload)
    request_id = res["ResponseMetadata"]["RequestId"]
    created_at_rfc1123 = res["ResponseMetadata"]["HTTPHeaders"]["date"]
    created_at = parsedate_to_datetime(created_at_rfc1123)
    print("lambda request_id:", request_id)
    print("lambda created_at:", created_at)
    handler.show_logs(request_id, created_at)


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
