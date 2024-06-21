import json
import os
from pathlib import Path

import boto3
from django.core.management import call_command

from ..context import Context
from ..utils import get_toml_path

# https://docs.djangoproject.com/en/5.0/ref/settings/#storages
default_storages = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

# https://docs.djangoproject.com/en/5.0/ref/settings/#caches
default_caches = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}


def get_storages(*, stage: str | None = None, path: str | Path | None = None) -> dict:
    stage = stage or os.environ.get("POCKET_STAGE")
    if not stage:
        return default_storages
    path = path or get_toml_path()
    context = Context.from_toml(stage=stage, path=path)
    if not (context.awscontainer and context.awscontainer.django):
        return default_storages
    storages = {}
    for key, storage in context.awscontainer.django.storages.items():
        storages[key] = {"BACKEND": storage.backend}
        if storage.store == "s3":
            assert context.s3, "Never happen because of context validation."
            storages[key]["OPTIONS"] = {
                "bucket_name": context.s3.bucket_name,
                "location": storage.location,
            }
        else:
            raise ValueError("Unknown store")
    return storages


def get_caches(*, stage: str | None = None, path: str | Path | None = None) -> dict:
    stage = stage or os.environ.get("POCKET_STAGE")
    if not stage:
        return default_caches
    path = path or get_toml_path()
    context = Context.from_toml(stage=stage, path=path)
    if not (context.awscontainer and context.awscontainer.django):
        return default_caches
    caches = {}
    for key, cache in context.awscontainer.django.caches.items():
        caches[key] = {
            "BACKEND": cache.backend,
            "LOCATION": cache.location,
        }
    return caches


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
