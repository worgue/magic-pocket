from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..general_context import GeneralContext
from ..runtime import (
    get_context,
    set_envs_from_aws_resources,
    set_envs_from_secrets,
)
from ..utils import get_toml_path


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


def get_django_settings(
    stage: str | None = None, path: str | Path | None = None
) -> dict[str, Any]:
    stage = stage or os.environ.get("POCKET_STAGE")
    path = path or get_toml_path()
    general_context = GeneralContext.from_toml(path=path)
    assert general_context.django_fallback, (
        "Never happen because of context validation."
    )
    if not stage:
        django_context = general_context.django_fallback
    else:
        context = get_context(stage=stage, path=path)
        if context.awscontainer and context.awscontainer.django:
            django_context = context.awscontainer.django
        else:
            django_context = general_context.django_fallback
    return django_context.settings
