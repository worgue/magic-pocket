import json
import os
from subprocess import run

from apig_wsgi import make_lambda_handler
from django.core.management import call_command

from pocket.django.utils import pocket_delete_sqs_task

from ..utils import get_wsgi_application

wsgi_handler = make_lambda_handler(
    get_wsgi_application(),
    binary_support=True,
    non_binary_content_type_prefixes=(
        "application/json",
        "application/vnd.api+json",
    ),
)


def management_command_handler(event, context):
    print(event)
    command = event["command"]
    args = event.get("args") or []
    kwargs = event.get("kwargs") or {}
    print(command)
    print("args:", args)
    print("kwargs:", kwargs)
    if command == "createsuperuser":
        if not os.environ.get("DJANGO_SUPERUSER_PASSWORD"):
            raise Exception("DJANGO_SUPERUSER_PASSWORD is not set")
    call_command(command, *args, **kwargs)


def sqs_management_command_handler(event, context):
    print(event)
    for record in event["Records"]:
        print(record["body"])
        data = json.loads(record["body"])
        call_command(data["command"], *data["args"], **data["kwargs"])
        pocket_delete_sqs_task(record["receiptHandle"])


def sqs_management_command_report_failuers_handler(event, context):
    print(event)
    batch_item_failures = []
    sqs_batch_response = {}
    for record in event["Records"]:
        print(record["body"])
        data = json.loads(record["body"])
        try:
            call_command(data["command"], *data["args"], **data["kwargs"])
            pocket_delete_sqs_task(record["receiptHandle"])
        except Exception as e:
            print(e)
            batch_item_failures.append({"itemIdentifier": record["messageId"]})
    sqs_batch_response["batchItemFailures"] = batch_item_failures
    return sqs_batch_response


def shell_handler(event, context):
    print(event)
    command_line = event["command_line"]
    run(command_line, shell=True, check=True)
