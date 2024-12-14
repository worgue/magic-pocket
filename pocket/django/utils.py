import json
import os
from pathlib import Path

import boto3
from django.core.management import call_command

from ..context import Context
from ..general_context import GeneralContext
from ..runtime import get_context
from ..utils import get_toml_path


def get_storages(*, stage: str | None = None, path: str | Path | None = None) -> dict:
    stage = stage or os.environ.get("POCKET_STAGE")
    path = path or get_toml_path()
    general_context = GeneralContext.from_toml(path=path)
    context: Context | None = None
    if not stage:
        django_context = general_context.django_fallback
        assert django_context, "Never happen because of context validation."
    else:
        context = get_context(stage=stage, path=path)
        if not (
            context.awscontainer
            and context.awscontainer.django
            and context.awscontainer.django.storages
        ):
            django_context = general_context.django_fallback
            assert django_context, "Never happen because of context validation."
        else:
            django_context = context.awscontainer.django
    storages = {}
    for key, storage in django_context.storages.items():
        storages[key] = {"BACKEND": storage.backend}
        if storage.store == "s3":
            if context:
                assert context.s3, "Never happen because of context validation."
                bucket_name = context.s3.bucket_name
            else:
                assert (
                    general_context.s3_fallback_bucket_name
                ), "use context validation."
                bucket_name = general_context.s3_fallback_bucket_name
            storages[key]["OPTIONS"] = {
                "bucket_name": bucket_name,
                "location": storage.location,
            }
        elif storage.store == "cloudfront":
            if context:
                assert context.cloudfront, "Never happen because of context validation."
                bucket_name = context.cloudfront.bucket_name
                route = context.cloudfront.get_route(storage.options["cloudfront_ref"])
                location = context.cloudfront.origin_prefix + route.path_pattern
                assert location[0] == "/"
                location = location[1:]
                domain = context.cloudfront.domain
                storages[key]["OPTIONS"] = {
                    "bucket_name": bucket_name,
                    "location": location,
                    "querystring_auth": False,
                    "custom_domain": domain,
                }
            else:
                raise ValueError("context is required for cloudfront storage")
        elif storage.store == "filesystem":
            if storage.location is not None:
                storages[key]["OPTIONS"] = {"location": storage.location}
        else:
            raise ValueError("Unknown store")
        if storage.options:
            if "OPTIONS" not in storages[key]:
                storages[key]["OPTIONS"] = {}
            storages[key]["OPTIONS"] = {**storages[key]["OPTIONS"], **storage.options}
    return storages


def get_caches(*, stage: str | None = None, path: str | Path | None = None) -> dict:
    stage = stage or os.environ.get("POCKET_STAGE")
    path = path or get_toml_path()
    general_context = GeneralContext.from_toml(path=path)
    assert (
        general_context.django_fallback
    ), "Never happen because of context validation."
    if not stage:
        django_context = general_context.django_fallback
    else:
        context = get_context(stage=stage, path=path)
        if not (
            context.awscontainer
            and context.awscontainer.django
            and context.awscontainer.django.caches
        ):
            django_context = general_context.django_fallback
        else:
            django_context = context.awscontainer.django
    caches = {}
    for key, cache in django_context.caches.items():
        caches[key] = {"BACKEND": cache.backend}
        if cache.location is not None:
            caches[key]["LOCATION"] = cache.location
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
