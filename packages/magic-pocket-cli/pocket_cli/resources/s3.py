from __future__ import annotations

import json
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
    from pocket.context import S3Context


class S3:
    context: S3Context

    def __init__(self, context: S3Context) -> None:
        self.context = context
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
        self.ensure_policy()

    def ensure_exists(self):
        if self.exists():
            self.ensure_public_access_block()
            self.ensure_policy()
            return
        self.create()

    def delete(self):
        delete_bucket_with_contents(self.client, self.context.bucket_name)

    def update(self):
        self.ensure_public_access_block()
        self.ensure_policy()

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
        if self.public_access_block_require_update or self.bucket_policy_require_update:
            return "REQUIRE_UPDATE"
        return "COMPLETED"

    @cached_property
    def bucket_policy(self):
        try:
            return json.loads(
                self.client.get_bucket_policy(Bucket=self.context.bucket_name)["Policy"]
            )
        except ClientError:
            return None

    @property
    def bucket_policy_should_be(self):
        public_resource = [
            "arn:aws:s3:::%s/%s/*" % (self.context.bucket_name, dirname)
            for dirname in self.context.public_dirs
        ]
        if len(public_resource) == 1:
            public_resource = public_resource[0]
        if public_resource:
            return {
                "Version": "2008-10-17",
                "Statement": [
                    {
                        "Sid": "PublicRead",
                        "Effect": "Allow",
                        "Principal": {"AWS": "*"},
                        "Action": "s3:GetObject",
                        "Resource": public_resource,
                    }
                ],
            }
        return None

    @property
    def bucket_policy_require_update(self):
        return self.bucket_policy_should_be != self.bucket_policy

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
            "BlockPublicPolicy": not bool(self.context.public_dirs),
            "RestrictPublicBuckets": not bool(self.context.public_dirs),
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

    def ensure_policy(self):
        if self.bucket_policy_require_update:
            echo.info("Update bucket policy required.")
            echo.info("Current policy: %s" % self.bucket_policy)
            if self.bucket_policy_should_be is None:
                self.client.delete_bucket_policy(Bucket=self.context.bucket_name)
                echo.info("Deleted bucket policy")
            else:
                self.client.put_bucket_policy(
                    Bucket=self.context.bucket_name,
                    Policy=json.dumps(self.bucket_policy_should_be),
                )
                echo.info("Updated policy: %s" % self.bucket_policy_should_be)
            del self.bucket_policy
        else:
            echo.info("Bucket policy is already configured properly.")
