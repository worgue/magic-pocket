import json
from email.utils import parsedate_to_datetime

import click

from ..context import Context
from . import django_installed


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
