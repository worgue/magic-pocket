from __future__ import annotations

import os

from ..runtime import set_env_from_resources


def add_or_append_env(key: str, value: str):
    if key not in os.environ:
        os.environ[key] = value
    else:
        os.environ[key] += "," + value


def set_django_env(stage: str):
    if not os.environ.get("POCKET_RESOURCES_ENV_LOADED"):
        set_env_from_resources(stage)
    add_or_append_env("ALLOWED_HOSTS", os.environ["POCKET_HOSTS"])
