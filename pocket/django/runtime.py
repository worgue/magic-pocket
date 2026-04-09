from __future__ import annotations

import os
from typing import Any

from ..general_context import GeneralContext
from ..runtime import (
    get_context,
    set_envs_from_aws_resources,
    set_envs_from_secrets,
)


def set_envs():
    set_envs_from_resources()
    set_envs_from_secrets()


def add_or_append_env(key: str, value: str):
    if key not in os.environ:
        os.environ[key] = value
    else:
        os.environ[key] += "," + value


def set_envs_from_resources(stage: str | None = None):
    stage = stage or os.environ.get("POCKET_STAGE")
    if not stage:
        return
    set_envs_from_aws_resources(stage)
    add_or_append_env("ALLOWED_HOSTS", os.environ["POCKET_HOSTS"])
    # CloudFront ドメインを ALLOWED_HOSTS / CSRF_TRUSTED_ORIGINS に追加
    context = get_context(stage=stage)
    if context.cloudfront:
        add_or_append_env("ALLOWED_HOSTS", ".cloudfront.net")
        add_or_append_env("CSRF_TRUSTED_ORIGINS", "https://*.cloudfront.net")
        for _name, cf_ctx in context.cloudfront.items():
            if cf_ctx.domain:
                add_or_append_env("ALLOWED_HOSTS", cf_ctx.domain)
                add_or_append_env("CSRF_TRUSTED_ORIGINS", f"https://{cf_ctx.domain}")


def get_django_settings(
    stage: str | None = None,
) -> dict[str, Any]:
    stage = stage or os.environ.get("POCKET_STAGE")
    general_context = GeneralContext.from_toml()
    assert general_context.django_fallback, (
        "Never happen because of context validation."
    )
    if not stage:
        django_context = general_context.django_fallback
        return django_context.settings
    context = get_context(stage=stage)
    if context.awscontainer and context.awscontainer.django:
        django_context = context.awscontainer.django
    else:
        django_context = general_context.django_fallback
    result = dict(django_context.settings)
    # CloudFront の API ルートがある場合、X-Forwarded-Host を有効化
    has_api_route = any(
        cf_ctx.has_any_api_route for cf_ctx in context.cloudfront.values()
    )
    if has_api_route:
        result.setdefault("USE_X_FORWARDED_HOST", True)
    return result
