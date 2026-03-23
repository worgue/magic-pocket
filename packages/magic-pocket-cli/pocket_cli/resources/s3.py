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

    def ensure_exists(self):
        if self.exists():
            self.ensure_public_access_block()
            self._ensure_cors()
            return
        self.create()

    def delete(self):
        delete_bucket_with_contents(self.client, self.context.bucket_name)

    def update(self):
        self.ensure_public_access_block()
        self._ensure_cors()

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
        return "COMPLETED"

    @cached_property
    def public_access_block(self):
        res = self.client.get_public_access_block(
            Bucket=self.context.bucket_name,
        )["PublicAccessBlockConfiguration"]
        return res

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

    def _ensure_cors(self):
        """S3 バケットの CORS 設定を適用する"""
        if not self.context.cors:
            return
        origins = self._resolve_cors_origins()
        if not origins:
            return
        cors_rules = [
            {
                "AllowedOrigins": origins,
                "AllowedMethods": self.context.cors.methods,
                "AllowedHeaders": ["*"],
                "MaxAgeSeconds": 3600,
            }
        ]
        self.client.put_bucket_cors(
            Bucket=self.context.bucket_name,
            CORSConfiguration={"CORSRules": cors_rules},
        )
        echo.info("CORS 設定を適用しました: %s" % origins)
