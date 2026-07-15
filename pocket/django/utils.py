import json
import os
import urllib.parse
from typing import Any

import boto3
from django.core.management import call_command

from ..context import Context
from ..general_context import GeneralContext
from ..runtime import get_context
from .db_url import parse_database_url_credentials


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
        if not django_context:
            raise RuntimeError("Never happen because of context validation.")
        return django_context
    if (
        context.awscontainer
        and context.awscontainer.django
        and context.awscontainer.django.storages
    ):
        return context.awscontainer.django
    django_context = general_context.django_fallback
    if not django_context:
        raise RuntimeError("Never happen because of context validation.")
    return django_context


def _resolve_route(cf, storage):
    """distribution 内のルートを解決する"""
    if storage.route:
        return cf.get_route(storage.route)
    return cf.default_route


def _create_cloudfront_signer(signing_key_name: str):
    """環境変数からCloudFrontSignerを生成。キーが未設定の場合は None を返す"""
    import base64
    import os

    from botocore.signers import CloudFrontSigner
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key_id = os.environ.get(f"{signing_key_name}_ID")
    pem_b64 = os.environ.get(f"{signing_key_name}_PEM_BASE64")
    if not key_id or not pem_b64:
        return None
    pem = base64.b64decode(pem_b64)
    private_key = serialization.load_pem_private_key(pem, password=None)

    def rsa_signer(message):
        return private_key.sign(message, padding.PKCS1v15(), hashes.SHA1())  # type: ignore  # noqa: S303 CloudFront 署名は AWS 仕様で RSA-SHA1 必須  # nosemgrep

    return CloudFrontSigner(key_id, rsa_signer)


def _resolve_cloudfront_domain(cf_name: str, cf_domain: str | None) -> str | None:
    """CloudFront ドメインを解決する。

    pocket.toml に domain が設定されていればそれを使い、
    未設定なら環境変数 POCKET_CLOUDFRONT_{NAME}_DOMAIN にフォールバックする。
    """
    if cf_domain:
        return cf_domain
    return os.environ.get("POCKET_CLOUDFRONT_%s_DOMAIN" % cf_name.upper())


def _resolve_s3_direct_options(
    storage, context: Context | None, general_context: GeneralContext
) -> dict:
    """S3 直接ストレージ（ローカル開発用）の bucket_name / location を解決する。"""
    if context:
        if not context.s3:
            raise RuntimeError("Never happen because of context validation.")
        bucket_name = context.s3.bucket_name
    else:
        if not general_context.s3_fallback_bucket_name:
            raise RuntimeError(
                "S3 storage is configured but "
                "s3_fallback_bucket_name is not set in [general]. "
                "Add it for local development, or set POCKET_STAGE."
            )
        bucket_name = general_context.s3_fallback_bucket_name
    return {"bucket_name": bucket_name, "location": storage.location}


def _build_storage_options(
    storage, context: Context | None, general_context: GeneralContext
) -> dict | None:
    if storage.store == "s3":
        if storage.distribution:
            # CloudFront 経由
            if not context:
                raise ValueError("context is required for distribution storage")
            cf = context.cloudfront[storage.distribution]
            route = _resolve_route(cf, storage)
            # S3 location = origin_path + path_pattern から自動計算
            s3_location = (route.origin_path + route.path_pattern.rstrip("/*")).lstrip(
                "/"
            )
            custom_domain = _resolve_cloudfront_domain(cf.name, cf.domain)
            options: dict = {
                "bucket_name": cf.bucket_name,
                "location": s3_location,
                "custom_domain": custom_domain,
                "custom_origin_path": route.origin_path,
                "querystring_auth": route.signed,
            }
            if route.signed and cf.signing_key:
                signer = _create_cloudfront_signer(cf.signing_key)
                if signer:
                    options["cloudfront_signer"] = signer
            return options
        else:
            # S3 直接（ローカル開発用）
            return _resolve_s3_direct_options(storage, context, general_context)
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
        # deploy_hash の staticfiles は StaticFilesStorage なので
        # S3 用 OPTIONS (bucket_name 等) を渡してはいけない
        if storage.deploy_hash and storage.static:
            continue
        options = _build_storage_options(storage, context, general_context)
        if options is not None:
            storages[key]["OPTIONS"] = options
        if storage.options:
            if "OPTIONS" not in storages[key]:
                storages[key]["OPTIONS"] = {}
            storages[key]["OPTIONS"] = {**storages[key]["OPTIONS"], **storage.options}
    return storages


def get_static_storage(*, stage: str | None = None):
    storages = get_storages(stage=stage)
    return storages["staticfiles"]  # must be available


def get_static_storage_s3_options(*, stage: str | None = None) -> dict:
    """staticfiles の S3 アップロード先情報を返す。

    deploy_hash モードでは get_storages() が StaticFilesStorage を返すため
    bucket_name 等が含まれない。この関数は storage 設定の原情報から
    S3 の bucket_name / location を常に取得する (deploystatic 用)。
    """
    stage = stage or os.environ.get("POCKET_STAGE")
    general_context, context = _get_django_context_for_storages(stage)
    django_context = _resolve_storage_django_context(general_context, context)
    storage = django_context.storages.get("staticfiles")
    if not storage or storage.store != "s3":
        raise ValueError("deploystatic requires staticfiles with store = 's3'")
    options = _build_storage_options(storage, context, general_context)
    if options is None:
        raise ValueError("Failed to build S3 options for staticfiles")
    return options


def get_email_backend(*, stage: str | None = None) -> dict[str, Any]:
    stage = stage or os.environ.get("POCKET_STAGE")
    if not stage:
        return {}
    from ..runtime import get_context

    context = get_context(stage=stage)
    if not context.ses:
        return {}
    ses = context.ses
    result: dict[str, Any] = {
        "EMAIL_BACKEND": "django_ses.SESBackend",
        "DEFAULT_FROM_EMAIL": ses.from_email,
        "AWS_SES_REGION_NAME": ses.region,
    }
    if ses.configuration_set:
        result["AWS_SES_CONFIGURATION_SET"] = ses.configuration_set
    return result


def get_caches(*, stage: str | None = None) -> dict:
    stage = stage or os.environ.get("POCKET_STAGE")
    general_context = GeneralContext.from_toml()
    if not general_context.django_fallback:
        raise RuntimeError("Never happen because of context validation.")
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
        if cache.store == "redis":
            redis_url = os.environ.get("REDIS_URL", "")
            caches[key]["LOCATION"] = redis_url
            caches[key]["OPTIONS"] = {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
            }
        elif cache.location is not None:
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
        **parse_database_url_credentials(database_url),
    }
    if engine == "django_tidb":
        db["OPTIONS"] = {
            "ssl_mode": "VERIFY_IDENTITY",
            "ssl": {"ca": _tidb_ca_bundle_path()},
        }
        # Lambda は実行環境 (コンテナ) を再利用するため、持続接続で TLS
        # handshake を warm リクエストから省く。idle 切断された接続は再利用前の
        # health check で検知して張り直すので None (期限なし) でも安全。
        db["CONN_MAX_AGE"] = None
        db["CONN_HEALTH_CHECKS"] = True

    return {"default": db}


# システム CA バンドルの候補パス。distro ごとに配置が異なるため順に探索する。
# Lambda の base image (Amazon Linux 2023 / RHEL 系) は ca-bundle.crt、
# Debian/Ubuntu 系 (ローカル開発環境を含む) は ca-certificates.crt に置く。
_TIDB_CA_BUNDLE_CANDIDATES = (
    "/etc/pki/tls/certs/ca-bundle.crt",  # Amazon Linux 2023 / RHEL 系
    "/etc/ssl/certs/ca-certificates.crt",  # Debian / Ubuntu
)


def _tidb_ca_bundle_path() -> str:
    for path in _TIDB_CA_BUNDLE_CANDIDATES:
        if os.path.exists(path):
            return path
    # どの候補も無い環境では実行基盤である AL2023 の標準パスを返す
    # (存在チェックに失敗した場合の最後の拠り所)。
    return _TIDB_CA_BUNDLE_CANDIDATES[0]


def _detect_engine(stage: str | None, scheme: str) -> str:
    if stage:
        context = get_context(stage=stage)
        if context.tidb:
            return "django_tidb"
        if context.rds:
            # master password ローテーションに追従する RDS 専用 backend。
            return "pocket.django.db_backends.rds"
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
