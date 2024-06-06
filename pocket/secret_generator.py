from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from click import echo

from pocket.resources.neon import NeonResourceIsNotReady

if TYPE_CHECKING:
    from pocket.context import Context
    from pocket.settings import PocketSecret


def generate_secret(context: Context, pocket_secret: PocketSecret) -> str | None:
    if pocket_secret.type == "password":
        length = pocket_secret.options.get("length", 16)
        if not isinstance(length, int):
            raise Exception("length must be integer")
        chars = pocket_secret.options.get(
            # default is compatible with Django's SECRET_KEY
            "chars",
            "abcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*(-_=+)",
        )
        if not isinstance(chars, str):
            raise Exception("chars must be string")
        return "".join(secrets.choice(chars) for _ in range(length))
    elif pocket_secret.type == "neon_database_url":
        if not context.neon:
            raise Exception("neon is not configured. Please set neon in pocket.toml")
        try:
            return context.neon.resource.database_url
        except NeonResourceIsNotReady:
            echo.warning("neon database is not ready")
            return None
    else:
        raise Exception("Unknown secret type: %s" % pocket_secret.type)
