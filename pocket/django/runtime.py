from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..context import Context
from ..runtime import set_env_from_resources


def add_or_append_env(key: str, value: str):
    if key not in os.environ:
        os.environ[key] = value
    else:
        os.environ[key] += "," + value


def set_django_env(stage: str | None = None):
    stage = stage or os.environ.get("POCKET_STAGE")
    if not stage:
        return
    if not os.environ.get("POCKET_RESOURCES_ENV_LOADED"):
        set_env_from_resources(stage)
    add_or_append_env("ALLOWED_HOSTS", os.environ["POCKET_HOSTS"])


def get_django_settings(
    stage: str | None = None, path: str | Path | None = None
) -> dict[str, Any]:
    stage = stage or os.environ.get("POCKET_STAGE")
    if not stage:
        return {}
    context = Context.from_toml(stage=stage, path=path or "pocket.toml")
    if context.awscontainer and context.awscontainer.django:
        return context.awscontainer.django.settings
    return {}
