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
                if managed_secret.type in (
                    "neon_database_url",
                    "tidb_database_url",
                    "upstash_redis_url",
                ):
                    echo.warning(
                        "computed DB/KVS URL (managed type=%s) は deprecated です。"
                        "deploy が管理 API を叩いて URL を算出する方式から、"
                        "[<db>] provisioning + [awscontainer.secrets.user] の type + "
                        "`pocket <db> store-url` (stored) への移行を推奨します。"
                        % managed_secret.type
                    )
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
        self.verify_user_stored_secrets()
        if self.context.awscontainer and self.context.awscontainer.secrets:
            sc = self.context.awscontainer.secrets
            if hasattr(sc, "pocket_store"):
                del sc.pocket_store
            if hasattr(sc, "allowed_sm_resources"):
                del sc.allowed_sm_resources
            if hasattr(sc, "allowed_ssm_resources"):
                del sc.allowed_ssm_resources

    def verify_user_stored_secrets(self):
        """type 付き user secret (stored mode) が deploy 前に provision 済みか検証する。

        computed (managed) と違い pocket は値を生成しないため、未 provision でも
        deploy は通り runtime まで遅延して落ちる。それを避けるため deploy 時に store を
        引いて存在を確認し、無ければ正準名を示して止める。管理 API は叩かない。
        """
        if self.context.awscontainer is None:
            return
        if (sc := self.context.awscontainer.secrets) is None:
            return
        missing: list[str] = []
        for key, spec in sc.user.items():
            # type 付き = stored mode のみ対象。name は from_settings で導出済み。
            if spec.type is None or spec.name is None:
                continue
            store = spec.store or sc.store
            if not self._stored_secret_exists(spec.name, store, sc.region):
                missing.append(
                    "  - %s (type=%s, store=%s): %s"
                    % (key, spec.type, store, spec.name)
                )
        if missing:
            raise RuntimeError(
                "stored mode の user secret が見つかりません。"
                "deploy 前に下記の secret を provision してください "
                "(値は接続 URL):\n" + "\n".join(missing)
            )

    def _stored_secret_exists(self, name: str, store: str, region: str) -> bool:
        import boto3
        from botocore.exceptions import ClientError

        try:
            if store == "ssm":
                boto3.client("ssm", region_name=region).get_parameter(Name=name)
            else:
                boto3.client("secretsmanager", region_name=region).describe_secret(
                    SecretId=name
                )
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("ParameterNotFound", "ResourceNotFoundException"):
                return False
            raise

    def stored_secret_exists(self, spec) -> bool:
        """stored mode user secret (spec.name) が store に存在するか。

        store-url の冪等判定用。
        """
        if (
            self.context.awscontainer is None
            or self.context.awscontainer.secrets is None
            or spec.name is None
        ):
            return False
        sc = self.context.awscontainer.secrets
        store = spec.store or sc.store
        return self._stored_secret_exists(spec.name, store, sc.region)

    def _require_secrets(self):
        """awscontainer.secrets (SecretsContext) を返す。未設定なら raise。"""
        ac = self.context.awscontainer
        if ac is None or ac.secrets is None:
            raise RuntimeError("awscontainer secrets is not configured")
        return ac.secrets

    def _put_stored_value(self, name: str, store: str, value: str, region: str) -> None:
        """正準名 (name) に単一値を書き込む (ssm=SecureString / sm=create|put)。"""
        import boto3
        from botocore.exceptions import ClientError

        if store == "ssm":
            boto3.client("ssm", region_name=region).put_parameter(
                Name=name, Value=value, Type="SecureString", Overwrite=True
            )
            return
        client = boto3.client("secretsmanager", region_name=region)
        try:
            client.create_secret(
                Name=name,
                SecretString=value,
                Tags=[{"Key": "Name", "Value": name}],
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceExistsException":
                client.put_secret_value(SecretId=name, SecretString=value)
            else:
                raise

    def _read_stored_value(self, name: str, store: str, region: str) -> str | None:
        """正準名 (name) の値を読む。未 provision (NotFound) なら None。"""
        import boto3
        from botocore.exceptions import ClientError

        try:
            if store == "ssm":
                res = boto3.client("ssm", region_name=region).get_parameter(
                    Name=name, WithDecryption=True
                )
                return res["Parameter"]["Value"]
            res = boto3.client("secretsmanager", region_name=region).get_secret_value(
                SecretId=name
            )
            return res["SecretString"]
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("ParameterNotFound", "ResourceNotFoundException"):
                return None
            raise

    def _delete_stored_value(self, name: str, store: str, region: str) -> None:
        """正準名 (name) を削除する (migrate の旧パス cleanup 用)。"""
        import boto3

        if store == "ssm":
            boto3.client("ssm", region_name=region).delete_parameter(Name=name)
        else:
            boto3.client("secretsmanager", region_name=region).delete_secret(
                SecretId=name
            )

    def store_user_secret(self, spec, value: str) -> None:
        """stored mode user secret の正準名 (spec.name) に単一値を書き込む。

        `pocket <db> store-url` から使う。pocket_store (managed 集約) ではなく
        user secret の type 基準導出名 ({pocket_key}-user/{type}) に直接 put する。
        読み側 _stored_secret_exists と対称。
        """
        if (
            self.context.awscontainer is None
            or self.context.awscontainer.secrets is None
        ):
            raise RuntimeError("awscontainer secrets is not configured")
        sc = self.context.awscontainer.secrets
        if spec.name is None:
            raise RuntimeError("user secret name is not resolved")
        store = spec.store or sc.store
        self._put_stored_value(spec.name, store, value, sc.region)

    def read_user_secret(self, spec) -> str | None:
        """stored mode user secret (spec.name) の値を読む。未 provision なら None。

        store_user_secret / _stored_secret_exists と対称の読み取り。
        `pocket <db> url` の stored-first 解決に使う。管理 API は叩かない。
        """
        if (
            self.context.awscontainer is None
            or self.context.awscontainer.secrets is None
            or spec.name is None
        ):
            return None
        sc = self.context.awscontainer.secrets
        store = spec.store or sc.store
        return self._read_stored_value(spec.name, store, sc.region)

    def read_stored_url_by_type(self, secret_type: str) -> str | None:
        """type 基準の正準パスを (consumer 宣言なしで) 構築して stored URL を読む。

        dual-declaration で consumer (DATABASE_URL) が別 backend を指していても、
        「その backend の stored URL」を type から直接引ける。store は secrets.store
        既定 (per-type override は宣言でしか表現できないため)。
        """
        if (
            self.context.awscontainer is None
            or self.context.awscontainer.secrets is None
        ):
            return None
        sc = self.context.awscontainer.secrets
        name = sc.stored_url_name(secret_type)
        return self._read_stored_value(name, sc.store, sc.region)

    def migrate_user_secret_path(
        self, key: str, spec, *, dry_run: bool = False
    ) -> dict:
        """0.11→0.12: user secret を旧キー基準パス→新 type 基準パスへ移設する。

        旧 /{pocket_key}-user/{key} の値を新 /{pocket_key}-user/{type} へ copy し、
        verify 後に旧を delete する。冪等 (新在+旧在なら旧のみ delete して cleanup を
        完了)。戻り値の status: ``migrated`` / ``cleaned`` / ``already`` / ``missing``
        / ``skip-name`` (dry_run では ``would-migrate`` / ``would-clean``)。
        """
        from pocket.context import user_secret_path

        sc = self._require_secrets()
        if spec.type is None:  # name モードは移設対象外
            return {"status": "skip-name", "key": key}
        store = spec.store or sc.store
        old_name = user_secret_path(sc.pocket_key, key, store)
        new_name = sc.stored_url_name(spec.type, store)
        info = {
            "status": None,
            "key": key,
            "type": spec.type,
            "old": old_name,
            "new": new_name,
        }
        if old_name == new_name:  # 既に type == key (レア) なら移設不要
            info["status"] = "already"
            return info
        region = sc.region
        new_exists = self._stored_secret_exists(new_name, store, region)
        old_exists = self._stored_secret_exists(old_name, store, region)
        if new_exists:
            if old_exists:
                if not dry_run:
                    self._delete_stored_value(old_name, store, region)
                info["status"] = "would-clean" if dry_run else "cleaned"
            else:
                info["status"] = "already"
            return info
        if not old_exists:
            info["status"] = "missing"
            return info
        if dry_run:
            info["status"] = "would-migrate"
            return info
        value = self._read_stored_value(old_name, store, region)
        if value is None:
            info["status"] = "missing"
            return info
        self._put_stored_value(new_name, store, value, region)
        if self._read_stored_value(new_name, store, region) != value:
            raise RuntimeError("copy verify failed: %s" % new_name)
        self._delete_stored_value(old_name, store, region)
        info["status"] = "migrated"
        return info

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
        elif spec.type in ("spa_token_secret", "origin_verify_secret"):
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
