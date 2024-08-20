from __future__ import annotations

import json
from functools import cached_property
from typing import TYPE_CHECKING, Literal

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel

from ..utils import echo
from .aws.cloudformation import SpaStack
from .base import ResourceStatus

if TYPE_CHECKING:
    from pocket.context import SpaContext


class OriginAccessControl(BaseModel):
    Id: str
    Description: str | None = None
    Name: str
    SigningProtocol: Literal["sigv4"]
    SigningBehavior: Literal["never", "always", "no-override"]
    OriginAccessControlOriginType: Literal[
        "s3", "mediastore", "mediapackagev2", "lambda"
    ]


class NoOacException(Exception):
    pass


class Spa:
    context: SpaContext

    def __init__(self, context: SpaContext) -> None:
        self.context = context
        self.s3_client = boto3.client("s3", region_name=context.region)
        self.cf_client = boto3.client("cloudfront", region_name=context.region)

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
        self.update()

    def update(self):
        if not self._s3_exists():
            self._create_s3_bucket()
        if not self.stack.exists:
            self.stack.create()
        elif not self.stack.yaml_synced:
            self.stack.update()
        w = echo.warning
        w("Waiting for cloudformation stack to be completed ...")
        w("This may take a few minutes.")
        w("Because cloudfront distribution id is required to set s3 bucket policy.")
        w("If you want to come back later, you can safely cancel this process.")
        w("Please run `pocket resource spa update` later.")
        self.stack.wait_status("COMPLETED")
        self._ensure_bucket_policy()

    def delete(self):
        self._delete_bucket_policy()
        self.stack.delete()
        echo.info("Deleting cloudformation stack for spa ...")
        echo.warning("Please delete the bucket resources manually.")
        echo.warning("The bucket name: " + self.context.bucket_name)

    def _create_s3_bucket(self):
        if self.context.region == "us-east-1":
            self.s3_client.create_bucket(Bucket=self.context.bucket_name)
        else:
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
        if self.bucket_policy_require_update:
            echo.info("Update bucket policy required.")
            echo.info("Current policy: %s" % self.bucket_policy)
            if self.bucket_policy_should_be is None:
                self.s3_client.delete_bucket_policy(Bucket=self.context.bucket_name)
                echo.info("Deleted bucket policy")
            else:
                self.s3_client.put_bucket_policy(
                    Bucket=self.context.bucket_name,
                    Policy=json.dumps(self.bucket_policy_should_be),
                )
                echo.info("Updated policy: %s" % self.bucket_policy_should_be)
            del self.bucket_policy
        else:
            echo.info("Bucket policy is already configured properly.")

    def _delete_bucket_policy(self):
        self.s3_client.delete_bucket_policy(Bucket=self.context.bucket_name)

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
        return boto3.client("sts").get_caller_identity().get("Account")

    @cached_property
    def distribution_id(self):
        if not self.stack.output:
            raise Exception("Cloudfront distribution is not created yet.")
        return self.stack.output["DistributionId"]

    @property
    def bucket_policy_should_be(self):
        return {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "AllowCloudFrontServicePrincipalReadOnly",
                    "Effect": "Allow",
                    "Principal": {"Service": "cloudfront.amazonaws.com"},
                    "Action": "s3:GetObject",
                    "Resource": "arn:aws:s3:::%s/*" % self.context.bucket_name,
                    "Condition": {
                        "StringEquals": {
                            "AWS:SourceArn": "arn:aws:cloudfront::%s:distribution/%s"
                            % (self.account_id, self.distribution_id)
                        }
                    },
                }
            ],
        }

    @property
    def url_fallback_function_indent8(self):
        lines = []
        for i, line in enumerate(self._url_fallback_function.splitlines()):
            if i == 0:
                lines.append(line)
            else:
                lines.append(" " * 8 + line)
        return "\n".join(lines)

    @property
    def _url_fallback_function(self):
        return (
            """function handler(event) {
    var request = event.request;
    var lastItem = request.uri.split('/').pop();
    if (!lastItem.includes('.')) {
        request.uri = '/%s';
    }
    return request;
}
"""
            % self.context.fallback_html
        )
