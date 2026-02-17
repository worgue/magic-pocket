from __future__ import annotations

import base64
import secrets
from typing import TYPE_CHECKING, Literal

from pocket.resources.neon import NeonResourceIsNotReady

from .utils import echo

if TYPE_CHECKING:
    from pocket.context import Context
    from pocket.settings import ManagedSecretSpec


class Mediator:
    """Do some tasks that requires access to mulple resources."""

    ErrorLevel = Literal["ignore", "warning", "raise"]

    def __init__(self, context: Context) -> None:
        self.context = context

    def _conditional_error(self, level: ErrorLevel, msg: str):
        if level == "ignore":
            return
        elif level == "warning":
            echo.warning(msg)
            return
        else:
            raise Exception(msg)

    def create_pocket_managed_secrets(
        self, exists: ErrorLevel = "warning", failed: ErrorLevel = "raise"
    ):
        if self.context.awscontainer is None:
            return
        if (sc := self.context.awscontainer.secrets) is None:
            return
        generated: dict[str, str | dict[str, str]] = {}
        for key, managed_secret in sc.managed.items():
            if key not in sc.pocket_store.secrets:
                value = self._generate_secret(managed_secret)
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
            new_pocket_secrets = sc.pocket_store.secrets.copy() | generated
            sc.pocket_store.update_secrets(new_pocket_secrets)

    def ensure_pocket_managed_secrets(self):
        self.create_pocket_managed_secrets(exists="ignore")
        if self.context.awscontainer and self.context.awscontainer.secrets:
            sc = self.context.awscontainer.secrets
            if hasattr(sc, "allowed_sm_resources"):
                del sc.allowed_sm_resources
            if hasattr(sc, "allowed_ssm_resources"):
                del sc.allowed_ssm_resources

    def _generate_secret(self, spec: ManagedSecretSpec):
        if spec.type == "password":
            return self._generate_password(spec.options)
        elif spec.type == "neon_database_url":
            return self._get_neon_database_url()
        elif spec.type == "rsa_pem_base64":
            return self._generate_rsa_pem()
        else:
            raise Exception("Unknown secret type: %s" % spec.type)

    def _generate_rsa_pem(self) -> dict[str, str]:
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
        except ModuleNotFoundError:
            echo.warning("cryptography is not installed.")
            echo.warning("Please install cryptography to generate RSA key pair.")
            echo.warning("rye add cryptography")
            raise
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem_private_key = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pem_public_key = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return {
            "pem": base64.b64encode(pem_private_key).decode("utf-8"),
            "pub": base64.b64encode(pem_public_key).decode("utf-8"),
        }

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
