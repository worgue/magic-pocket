import json
import os
import urllib.parse

import boto3
from django.core.management import call_command

from ..context import Context
from ..general_context import GeneralContext
from ..runtime import get_context


def _get_django_context_for_storages(
    stage: str | None,
) -> tuple[GeneralContext, Context | None]:
    general_context = GeneralContext.from_toml()
    context: Context | None = None
    if stage:
        context = get_context(stage=stage)
    return general_context, context


def _resolve_storage_django_context(
    general_context: GeneralContext, context: Context | None
):
    if not context:
        django_context = general_context.django_fallback
        assert django_context, "Never happen because of context validation."
        return django_context
    if (
        context.awscontainer
        and context.awscontainer.django
        and context.awscontainer.django.storages
    ):
        return context.awscontainer.django
    django_context = general_context.django_fallback
    assert django_context, "Never happen because of context validation."
    return django_context


def _build_storage_options(
    storage, context: Context | None, general_context: GeneralContext
) -> dict | None:
    if storage.store == "s3":
        if context:
            assert context.s3, "Never happen because of context validation."
            bucket_name = context.s3.bucket_name
        else:
            assert general_context.s3_fallback_bucket_name, (
                "S3 storage is configured but s3_fallback_bucket_name is not set "
                "in [general]. Add it for local development, or set POCKET_STAGE."
            )
            bucket_name = general_context.s3_fallback_bucket_name
        return {"bucket_name": bucket_name, "location": storage.location}
    elif storage.store == "cloudfront":
        if not context:
            raise ValueError("context is required for cloudfront storage")
        assert context.cloudfront, "Never happen because of context validation."
        route = context.cloudfront.get_route(storage.options["cloudfront_ref"])
        location = context.cloudfront.origin_prefix + route.path_pattern
        assert location[0] == "/"
        assert location[-2:] == "/*"
        location = location[1:-2]
        return {
            "bucket_name": context.cloudfront.bucket_name,
            "location": location,
            "querystring_auth": False,
            "custom_domain": context.cloudfront.domain,
            "custom_origin_path": context.cloudfront.origin_prefix,
        }
    elif storage.store == "filesystem":
        if storage.location is not None:
            return {"location": storage.location}
        return None
    raise ValueError("Unknown store")


def get_storages(*, stage: str | None = None) -> dict:
    stage = stage or os.environ.get("POCKET_STAGE")
    general_context, context = _get_django_context_for_storages(stage)
    django_context = _resolve_storage_django_context(general_context, context)
    storages = {}
    for key, storage in django_context.storages.items():
        if key == "staticfiles" and os.environ.get(
            "POCKET_STATICFILES_BACKEND_OVERRIDE"
        ):
            backend = os.environ["POCKET_STATICFILES_BACKEND_OVERRIDE"]
            location = os.environ.get("POCKET_STATICFILES_LOCATION_OVERRIDE")
            storages[key] = {"BACKEND": backend, "OPTIONS": {"location": location}}
            continue
        storages[key] = {"BACKEND": storage.backend}
        options = _build_storage_options(storage, context, general_context)
        if options is not None:
            storages[key]["OPTIONS"] = options
        if storage.options:
            if "OPTIONS" not in storages[key]:
                storages[key]["OPTIONS"] = {}
            storages[key]["OPTIONS"] = {**storages[key]["OPTIONS"], **storage.options}
            if storage.store == "cloudfront":
                storages[key]["OPTIONS"].pop("cloudfront_ref")
    return storages


def get_static_storage(*, stage: str | None = None):
    storages = get_storages(stage=stage)
    return storages["staticfiles"]  # must be available


def get_caches(*, stage: str | None = None) -> dict:
    stage = stage or os.environ.get("POCKET_STAGE")
    general_context = GeneralContext.from_toml()
    assert general_context.django_fallback, (
        "Never happen because of context validation."
    )
    if not stage:
        django_context = general_context.django_fallback
    else:
        context = get_context(stage=stage)
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


def get_databases(*, stage: str | None = None) -> dict:
    stage = stage or os.environ.get("POCKET_STAGE")

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        from ..utils import get_toml_path

        toml_dir = os.path.dirname(str(get_toml_path()))
        return {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": os.path.join(toml_dir, "db.sqlite3"),
            }
        }

    parsed = urllib.parse.urlparse(database_url)
    engine = _detect_engine(stage, parsed.scheme)

    db: dict = {
        "ENGINE": engine,
        "NAME": parsed.path.lstrip("/"),
        "USER": urllib.parse.unquote(parsed.username or ""),
        "PASSWORD": urllib.parse.unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or ""),
    }
    if engine == "django_tidb":
        db["OPTIONS"] = {
            "ssl_mode": "VERIFY_IDENTITY",
            "ssl": {"ca": "/etc/ssl/certs/ca-certificates.crt"},
        }

    return {"default": db}


def _detect_engine(stage: str | None, scheme: str) -> str:
    if stage:
        context = get_context(stage=stage)
        if context.tidb:
            return "django_tidb"
        if context.neon:
            return "django.db.backends.postgresql"
    if scheme in ("postgres", "postgresql"):
        return "django.db.backends.postgresql"
    if scheme == "mysql":
        return "django.db.backends.mysql"
    return "django.db.backends.sqlite3"


_sqs_client = None


def _get_sqs_client():
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs")
    return _sqs_client


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
        _get_sqs_client().send_message(
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
    _get_sqs_client().delete_message(
        QueueUrl=queue_url,
        ReceiptHandle=receipt_handle,
    )
