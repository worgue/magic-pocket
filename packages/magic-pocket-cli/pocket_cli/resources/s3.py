from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from pocket.resources.base import ResourceStatus
from pocket.utils import echo
from pocket_cli.resources.aws.s3_utils import (
    bucket_exists,
    create_bucket,
    delete_bucket_with_contents,
)

if TYPE_CHECKING:
    from pocket.context import CloudFrontContext, S3Context


class S3:
    context: S3Context
    _cloudfront_contexts: dict[str, CloudFrontContext]

    def __init__(
        self,
        context: S3Context,
        cloudfront_contexts: dict[str, CloudFrontContext] | None = None,
    ) -> None:
        self.context = context
        self._cloudfront_contexts = cloudfront_contexts or {}
        self.client = boto3.client("s3", region_name=context.region)

    @property
    def description(self):
        return "Create bucket: %s" % self.context.bucket_name

    def state_info(self):
        return {"s3": {"bucket_name": self.context.bucket_name}}

    def deploy_init(self):
        pass

    def create(self):
        create_bucket(self.client, self.context.bucket_name, self.context.region)
        self.ensure_public_access_block()
        self._ensure_cors()
        self._ensure_versioning()
        self._ensure_lifecycle()

    def ensure_exists(self):
        if self.exists():
            self.ensure_public_access_block()
            self._ensure_cors()
            self._ensure_versioning()
            self._ensure_lifecycle()
            return
        self.create()

    def delete(self):
        delete_bucket_with_contents(self.client, self.context.bucket_name)

    def update(self):
        self.ensure_public_access_block()
        self._ensure_cors()
        self._ensure_versioning()
        self._ensure_lifecycle()

    def exists(self):
        try:
            return bucket_exists(self.client, self.context.bucket_name)
        except ClientError as e:
            raise Exception(
                "Bucket might be already used by other account."
                " Try another bucket_prefix."
            ) from e

    @property
    def status(self) -> ResourceStatus:
        if not self.exists():
            return "NOEXIST"
        if self.public_access_block_require_update:
            return "REQUIRE_UPDATE"
        if self.cors_require_update:
            return "REQUIRE_UPDATE"
        if self.versioning_require_update:
            return "REQUIRE_UPDATE"
        if self.lifecycle_require_update:
            return "REQUIRE_UPDATE"
        return "COMPLETED"

    @cached_property
    def public_access_block(self) -> dict | None:
        try:
            res = self.client.get_public_access_block(
                Bucket=self.context.bucket_name,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchPublicAccessBlockConfiguration":
                return None
            raise
        return res["PublicAccessBlockConfiguration"]

    @property
    def public_access_block_should_be(self):
        return {
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        }

    @property
    def public_access_block_require_update(self):
        return self.public_access_block_should_be != self.public_access_block

    def ensure_public_access_block(self):
        if self.public_access_block_require_update:
            echo.info("Update public access block configuration")
            echo.info("Current configuration: %s" % self.public_access_block)
            self.client.put_public_access_block(
                Bucket=self.context.bucket_name,
                PublicAccessBlockConfiguration=self.public_access_block_should_be,
            )
            del self.public_access_block
            echo.info("Updated to: %s" % self.public_access_block_should_be)
        else:
            echo.info("Public access block is already configured properly.")

    def _resolve_cors_origins(self) -> list[str]:
        """CORS の AllowedOrigins を CloudFront ドメインから解決する"""
        if not self.context.cors:
            return []
        origins: list[str] = []
        for cf_name in self.context.cors.cloudfront_names:
            cf_ctx = self._cloudfront_contexts.get(cf_name)
            if not cf_ctx:
                echo.warning("CORS: cloudfront '%s' が見つかりません" % cf_name)
                continue
            if cf_ctx.domain:
                origins.append("https://%s" % cf_ctx.domain)
            else:
                origins.append("https://*.cloudfront.net")
        return origins

    def _desired_cors_rules(self) -> list[dict] | None:
        """期待する CORS ルールを返す。CORS 未設定なら None。"""
        if not self.context.cors:
            return None
        origins = self._resolve_cors_origins()
        if not origins:
            return None
        return [
            {
                "AllowedOrigins": origins,
                "AllowedMethods": self.context.cors.methods,
                "AllowedHeaders": ["*"],
                "MaxAgeSeconds": 3600,
            }
        ]

    @cached_property
    def current_cors_rules(self) -> list[dict] | None:
        """現在の S3 バケット CORS ルールを返す。未設定なら None。"""
        try:
            res = self.client.get_bucket_cors(Bucket=self.context.bucket_name)
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchCORSConfiguration":
                return None
            raise
        return res.get("CORSRules")

    @property
    def cors_require_update(self) -> bool:
        # 宣言的: desired と current が一致しなければ drift
        return self._desired_cors_rules() != self.current_cors_rules

    def _ensure_cors(self):
        """S3 バケットの CORS 設定を宣言的に適用する。

        - cors 宣言あり: PutBucketCors で置き換え
        - cors 宣言なし & 現状あり: DeleteBucketCors
        - cors 宣言なし & 現状なし: 何もしない
        """
        desired = self._desired_cors_rules()
        current = self.current_cors_rules
        if desired == current:
            return
        if desired is None:
            self.client.delete_bucket_cors(Bucket=self.context.bucket_name)
            self.__dict__.pop("current_cors_rules", None)
            echo.info("CORS 設定を削除しました")
            return
        self.client.put_bucket_cors(
            Bucket=self.context.bucket_name,
            CORSConfiguration={"CORSRules": desired},
        )
        self.__dict__.pop("current_cors_rules", None)
        echo.info("CORS 設定を適用しました: %s" % desired[0]["AllowedOrigins"])

    @cached_property
    def current_versioning_status(self) -> str | None:
        """現在の S3 バケット versioning 状態 (Enabled / Suspended / None)。"""
        res = self.client.get_bucket_versioning(Bucket=self.context.bucket_name)
        return res.get("Status")

    @property
    def versioning_require_update(self) -> bool:
        """宣言的判定。

        - versioning=True: 現状が Enabled でなければ drift
        - versioning=False: 現状が Enabled なら drift
          (None / Suspended は "未有効化" として等価扱い、no-op)
        """
        current = self.current_versioning_status
        if self.context.versioning:
            return current != "Enabled"
        return current == "Enabled"

    def _ensure_versioning(self):
        """S3 バケットの versioning を宣言的に適用する。

        - versioning=True かつ現状 != Enabled: PutBucketVersioning(Enabled)
        - versioning=False かつ現状 == Enabled: PutBucketVersioning(Suspended)
        - その他: no-op (None と Suspended は "未有効化" として等価)

        S3 仕様上、一度 Enabled にしたバケットは Suspended にしか戻せない
        (バージョン情報は保持される)。docs に明記する。
        """
        current = self.current_versioning_status
        if self.context.versioning:
            if current == "Enabled":
                return
            self.client.put_bucket_versioning(
                Bucket=self.context.bucket_name,
                VersioningConfiguration={"Status": "Enabled"},
            )
            self.__dict__.pop("current_versioning_status", None)
            echo.info("Versioning を有効化しました")
            return
        if current != "Enabled":
            return
        self.client.put_bucket_versioning(
            Bucket=self.context.bucket_name,
            VersioningConfiguration={"Status": "Suspended"},
        )
        self.__dict__.pop("current_versioning_status", None)
        echo.info("Versioning を Suspended にしました")

    def _desired_lifecycle_rules(self) -> list[dict] | None:
        """期待する Lifecycle ルールを返す。空のとき None (= ルールなし)。"""
        if not self.context.lifecycle_rules:
            return None
        rules: list[dict] = []
        for rule in self.context.lifecycle_rules:
            rules.append(
                {
                    "ID": rule.id,
                    "Status": "Enabled",
                    "Filter": {"Prefix": rule.prefix},
                    "NoncurrentVersionExpiration": {
                        "NoncurrentDays": rule.noncurrent_version_expiration_days,
                    },
                }
            )
        return rules

    @cached_property
    def current_lifecycle_rules(self) -> list[dict] | None:
        """現在の S3 バケット Lifecycle ルール。未設定なら None。"""
        try:
            res = self.client.get_bucket_lifecycle_configuration(
                Bucket=self.context.bucket_name,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchLifecycleConfiguration":
                return None
            raise
        return res.get("Rules")

    @property
    def lifecycle_require_update(self) -> bool:
        # 宣言的: desired と current が一致しなければ drift
        return self._desired_lifecycle_rules() != self.current_lifecycle_rules

    def _ensure_lifecycle(self):
        """S3 バケットの Lifecycle 設定を宣言的に適用する。

        - lifecycle_rules 宣言あり: PutBucketLifecycleConfiguration で置き換え
        - lifecycle_rules 空 & 現状あり: DeleteBucketLifecycle
        - lifecycle_rules 空 & 現状なし: 何もしない
        """
        desired = self._desired_lifecycle_rules()
        current = self.current_lifecycle_rules
        if desired == current:
            return
        if desired is None:
            self.client.delete_bucket_lifecycle(Bucket=self.context.bucket_name)
            self.__dict__.pop("current_lifecycle_rules", None)
            echo.info("Lifecycle 設定を削除しました")
            return
        self.client.put_bucket_lifecycle_configuration(
            Bucket=self.context.bucket_name,
            LifecycleConfiguration={"Rules": desired},
        )
        self.__dict__.pop("current_lifecycle_rules", None)
        echo.info("Lifecycle ルールを適用しました: %d 件" % len(desired))
