from __future__ import annotations

import base64
import secrets
from typing import TYPE_CHECKING, Literal

from pocket.utils import echo
from pocket_cli.resources.neon import Neon, NeonResourceIsNotReady
from pocket_cli.resources.tidb import TiDb, TiDbResourceIsNotReady
from pocket_cli.resources.upstash import Upstash, UpstashResourceIsNotReady

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
        self._cleanup_orphaned_secrets()
        if self.context.awscontainer and self.context.awscontainer.secrets:
            sc = self.context.awscontainer.secrets
            if hasattr(sc, "pocket_store"):
                del sc.pocket_store
            if hasattr(sc, "allowed_sm_resources"):
                del sc.allowed_sm_resources
            if hasattr(sc, "allowed_ssm_resources"):
                del sc.allowed_ssm_resources

    def _cleanup_orphaned_secrets(self):
        """SSM/SM にあるが managed 定義にないシークレットを削除する"""
        if self.context.awscontainer is None:
            return
        if (sc := self.context.awscontainer.secrets) is None:
            return
        stored_keys = set(sc.pocket_store.secrets.keys())
        managed_keys = set(sc.managed.keys())
        orphaned = stored_keys - managed_keys
        if not orphaned:
            return
        echo.warning(
            "managed 定義にないシークレットを削除します: %s"
            % ", ".join(sorted(orphaned))
        )
        # managed に含まれるキーだけ残して再書き込み
        cleaned = {
            k: v for k, v in sc.pocket_store.secrets.items() if k in managed_keys
        }
        sc.pocket_store.delete_secrets()
        if cleaned:
            sc.pocket_store.update_secrets(cleaned)

    def _generate_secret(self, spec: ManagedSecretSpec):
        if spec.type == "auto_database_url":
            return self._get_auto_database_url()
        elif spec.type == "password":
            return self._generate_password(spec.options)
        elif spec.type == "neon_database_url":
            return self._get_neon_database_url()
        elif spec.type == "tidb_database_url":
            return self._get_tidb_database_url()
        elif spec.type == "rds_database_url":
            return self._get_rds_database_url()
        elif spec.type == "upstash_redis_url":
            return self._get_upstash_redis_url()
        elif spec.type == "rsa_pem_base64":
            return self._generate_rsa_pem()
        elif spec.type == "cloudfront_signing_key":
            return self._generate_rsa_pem()
        elif spec.type == "spa_token_secret":
            return secrets.token_hex(32)
        else:
            raise RuntimeError("Unknown secret type: %s" % spec.type)

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
            return Neon(self.context.neon).database_url
        except NeonResourceIsNotReady:
            echo.warning("neon database is not ready")
            return None

    def _get_upstash_redis_url(self):
        if not self.context.upstash:
            raise RuntimeError(
                "upstash is not configured. Please set upstash in pocket.toml"
            )
        try:
            return Upstash(self.context.upstash).redis_url
        except UpstashResourceIsNotReady:
            echo.warning("upstash redis is not ready")
            return None

    def _get_auto_database_url(self):
        """pocket.toml の DB 設定を自動検出して DATABASE_URL を生成する"""
        dbs = []
        if self.context.neon:
            dbs.append("neon")
        if self.context.tidb:
            dbs.append("tidb")
        if self.context.rds:
            dbs.append("rds")
        if len(dbs) == 0:
            raise RuntimeError(
                "auto_database_url: DB が設定されていません。"
                "[neon], [tidb], [rds] のいずれかを pocket.toml に追加してください。"
            )
        if len(dbs) > 1:
            raise RuntimeError(
                "auto_database_url: 複数の DB が設定されています: %s。"
                "neon_database_url, tidb_database_url, rds_database_url "
                "のいずれかを明示的に指定してください。" % ", ".join(dbs)
            )
        db = dbs[0]
        if db == "neon":
            return self._get_neon_database_url()
        if db == "tidb":
            return self._get_tidb_database_url()
        # rds: runtime の _set_rds_database_url に委譲
        return self._get_rds_database_url()

    def _get_rds_database_url(self):
        """RDS の DATABASE_URL は runtime で動的構築されるため、

        deploy 時には marker 値を返す。
        runtime の _set_rds_database_url が POCKET_RDS_SECRET_ARN から
        実際の DATABASE_URL を上書きする。
        """
        if not self.context.rds:
            raise RuntimeError("rds is not configured. Please set rds in pocket.toml")
        return "__rds_runtime__"

    def _get_tidb_database_url(self):
        if not self.context.tidb:
            raise RuntimeError("tidb is not configured. Please set tidb in pocket.toml")
        try:
            return TiDb(self.context.tidb).database_url
        except TiDbResourceIsNotReady:
            echo.warning("tidb database is not ready")
            return None
