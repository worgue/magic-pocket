from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Literal

from pocket.resources.neon import NeonResourceIsNotReady

from .utils import echo

if TYPE_CHECKING:
    from pocket.context import Context
    from pocket.settings import PocketSecret


class Mediator:
    """Do some tasks that requires access to mulple resources."""

    ErrorLevel = Literal["ignore", "warning", "raise"]

    def __init__(self, context: Context) -> None:
        self.context = context

    def _conditional_error(self, level: ErrorLevel, msg: str):
        if level == "ignore":
            return
        elif level == "warn":
            echo.warning(msg)
            return
        else:
            raise Exception(msg)

    def create_pocket_managed_secrets(
        self, exists: ErrorLevel = "warning", failed: ErrorLevel = "raise"
    ):
        if self.context.awscontainer is None:
            return
        if (sm := self.context.awscontainer.secretsmanager) is None:
            return
        generated = {}
        for key, pocket_secret in sm.pocket.items():
            if key not in sm.resource.pocket_secrets:
                value = self._generate_secret(pocket_secret)
                if value is None:
                    msg = "Secret generation for %s is failed." % key
                    self._conditional_error(failed, msg)
                else:
                    generated[key] = value
            else:
                msg = (
                    "%s is already created. "
                    "Use rotate-pocket-managed if you want to refresh the secrets" % key
                )
                self._conditional_error(exists, msg)
        if generated:
            new_pocket_secrets = sm.resource.pocket_secrets.copy() | generated
            sm.resource.update_pocket_secrets(new_pocket_secrets)

    def ensure_pocket_managed_secrets(self):
        self.create_pocket_managed_secrets(exists="ignore")

    def _generate_secret(self, pocket_secret: PocketSecret):
        if pocket_secret.type == "password":
            return self._generate_password(pocket_secret.options)
        elif pocket_secret.type == "neon_database_url":
            return self._get_neon_database_url()
        else:
            raise Exception("Unknown secret type: %s" % pocket_secret.type)

    def _generate_password(self, options):
        length = options.get("length", 16)
        if not isinstance(length, int):
            raise Exception("length must be integer")
        chars = options.get(
            # default is compatible with Django's SECRET_KEY
            "chars",
            "abcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*(-_=+)",
        )
        if not isinstance(chars, str):
            raise Exception("chars must be string")
        return "".join(secrets.choice(chars) for _ in range(length))

    def _get_neon_database_url(self):
        if not self.context.neon:
            raise Exception("neon is not configured. Please set neon in pocket.toml")
        try:
            return self.context.neon.resource.database_url
        except NeonResourceIsNotReady:
            echo.warning("neon database is not ready")
            return None
