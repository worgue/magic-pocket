from __future__ import annotations

import json
from functools import cached_property
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from .aws.cloudformation import SpaStack
from .base import ResourceStatus

if TYPE_CHECKING:
    from pocket.context import SpaContext


class Spa:
    context: SpaContext

    def __init__(self, context: SpaContext) -> None:
        self.context = context
        self.s3_client = boto3.client("s3", region_name=context.region)

    @property
    def description(self):
        return (
            "Create cloudformation(for cloudfront) and s3 bucket: %s"
            % self.context.bucket_name
        )

    def deploy_init(self):
        pass

    @property
    def status(self) -> ResourceStatus:
        if not self._s3_exists():
            return "NOEXIST"
        return self.stack.status

    @property
    def stack(self):
        return SpaStack(self.context)

    def create(self):
        if not self._s3_exists():
            self._create_s3_bucket()
        self.stack.create()
        # self._ensure_bucket_policy()

    def update(self):
        if not self._s3_exists():
            self._create_s3_bucket()
        if not self.stack.yaml_synced:
            self.stack.update()
        # self._ensure_bucket_policy()

    def _create_s3_bucket(self):
        self.s3_client.create_bucket(
            Bucket=self.context.bucket_name,
            CreateBucketConfiguration={
                "LocationConstraint": self.context.region,
            },
        )

    def _s3_exists(self):
        try:
            self.s3_client.head_bucket(Bucket=self.context.bucket_name)
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "404":
                return False
        raise Exception(
            "Bucket might be already used by other account. "
            "You may need to change the domain."
        )

    def _ensure_bucket_policy(self):
        policy = self.bucket_policy_should_be
        self.s3_client.put_bucket_policy(
            Bucket=self.context.domain,
            Policy=policy,
        )

    @property
    def bucket_policy_require_update(self):
        return self.bucket_policy_should_be != self.bucket_policy

    @cached_property
    def bucket_policy(self):
        try:
            return json.loads(
                self.s3_client.get_bucket_policy(Bucket=self.context.bucket_name)[
                    "Policy"
                ]
            )
        except ClientError:
            return None

    @cached_property
    def account_id(self):
        boto3.client("sts").get_caller_identity().get("Account")

    @property
    def bucket_policy_should_be(self):
        if not self.stack.output:
            raise Exception("Cloudfront distribution is not created yet.")
        distribution_id = self.stack.output["DistributionId"]
        return {
            "Version": "2012-10-17",
            "Statement": {
                "Sid": "AllowCloudFrontServicePrincipalReadOnly",
                "Effect": "Allow",
                "Principal": {"Service": "cloudfront.amazonaws.com"},
                "Action": "s3:GetObject",
                "Resource": "arn:aws:s3:::%s/*" % self.context.bucket_name,
                "Condition": {
                    "StringEquals": {
                        "AWS:SourceArn": "arn:aws:cloudfront::%s:distribution/%s"
                        % (self.account_id, distribution_id)
                    }
                },
            },
        }
