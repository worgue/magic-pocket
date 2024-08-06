from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pocket.global_context import GlobalContext

from ..context import Context
from ..runtime import set_env_from_resources
from ..utils import get_toml_path
from .utils import check_django_test


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
    path = path or get_toml_path()
    global_context = GlobalContext.from_toml(path=path)
    assert global_context.django_fallback, "Never happen because of context validation."
    if not stage:
        if check_django_test() and global_context.django_test:
            django_context = global_context.django_test
        else:
            django_context = global_context.django_fallback
    else:
        context = Context.from_toml(stage=stage, path=path)
        if context.awscontainer and context.awscontainer.django:
            django_context = context.awscontainer.django
        else:
            if check_django_test() and global_context.django_test:
                django_context = global_context.django_test
            else:
                django_context = global_context.django_fallback
    return django_context.settings
