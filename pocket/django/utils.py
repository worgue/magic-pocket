import json
import os

import boto3
from django.core.management import call_command

from ..context import Context


def get_storages(*, default=None, stage: str | None = None) -> dict:
    stage = stage or os.environ.get("POCKET_STAGE")
    storages = default or {}
    if not stage:
        return storages
    context = Context.from_toml(stage=stage)
    if not context.django or not context.django.storages:
        return storages
    for key, storage in context.django.storages.items():
        storages[key] = {"BACKEND": storage.backend}
        if storage.store == "s3":
            if not context.s3:
                raise ValueError("Never happen because of context validation.")
            storages[key]["OPTIONS"] = {
                "bucket_name": context.s3.bucket_name,
                "location": storage.location,
            }
        else:
            raise ValueError("Unknown store")
    return storages


sqs_client = boto3.client("sqs")


def pocket_call_command(
    command,
    args=None,
    kwargs=None,
    force_direct=False,
    force_sqs=False,
    queue_key="sqsmanagement",
):
    """
    Call Django management command directly or through SQS.
    Basically, if POCKET_SQSMANAGEMENT_QUEUEURL is set, send command to SQS.
    Else, call command directly.
    """
    if force_direct and force_sqs:
        raise Exception("force_direct and force_sqs cannot be True at the same time")
    args = args or []
    kwargs = kwargs or {}
    queue_url = os.environ.get("POCKET_%s_QUEUEURL" % queue_key.upper())
    use_sqs = force_sqs or queue_url
    if force_direct:
        use_sqs = False
    if use_sqs:
        if queue_url is None:
            raise Exception("POCKET_%s_QUEUEURL is not set." % queue_key.upper())
        sqs_client.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(
                {"command": command, "args": args, "kwargs": kwargs}
            ),
        )
    else:
        call_command(command, *args, **kwargs)


def pocket_delete_sqs_task(receipt_handle: str, queue_key="sqsmanagement"):
    queue_url = os.environ.get("POCKET_%s_QUEUEURL" % queue_key.upper())
    if queue_url is None:
        raise Exception("POCKET_%s_QUEUEURL is not set." % queue_key.upper())
    sqs_client.delete_message(
        QueueUrl=queue_url,
        ReceiptHandle=receipt_handle,
    )
