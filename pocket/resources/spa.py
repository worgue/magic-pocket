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


class BucketOwnershipException(Exception):
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
        self.warn_contents()

    @property
    def status(self) -> ResourceStatus:
        if not self._origin_s3_exists():
            return "NOEXIST"
        return self.stack.status

    @property
    def stack(self):
        return SpaStack(self.context)

    def create(self):
        self.update()

    def update(self):
        self._ensure_redirect_from()
        if not self._origin_s3_exists():
            self._create_origin_bucket()
        if not self.stack.exists:
            self.stack.create()
        elif not self.stack.yaml_synced:
            self.stack.update()
        info = echo.info
        log = echo.log
        log("Waiting for cloudformation stack to be completed ...")
        log("This may take a few minutes.")
        log("Because cloudfront distribution id is required to set s3 bucket policy.")
        info("If you want to come back later, you can safely cancel this process.")
        info("In that case, run `pocket resource spa update` later.")
        self.stack.wait_status("COMPLETED", timeout=600, interval=10)
        self._ensure_bucket_policy()
        log("Bucket for spa is ready.")
        self.warn_contents()

    def warn_contents(self):
        bucket = self.context.bucket_name
        origin = self.context.origin_path
        echo.warning("Upload spa files manually to s3://%s%s" % (bucket, origin))
        eg_cmd = "npx s3-spa-upload build %s --delete" % bucket
        if origin:
            eg_cmd += " --prefix %s" % origin[1:]
        echo.info("e.g) " + eg_cmd)

    def delete(self):
        self._delete_redirect_from()
        self._delete_bucket_policy()
        self.stack.delete()
        echo.info("Deleting cloudformation stack for spa ...")
        echo.warning("Please delete the bucket resources manually.")
        echo.warning("The bucket name: " + self.context.bucket_name)

    def _bucket_exists(self, bucket_name):
        try:
            self.s3_client.head_bucket(Bucket=bucket_name)
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "404":
                return False
        raise BucketOwnershipException(
            "Bucket might be already used by other account. "
            "You may need to change the domain."
        )

    def _bucket_assert_empty(self, bucket_name):
        res = self.s3_client.list_objects_v2(Bucket=bucket_name)
        if "Contents" in res:
            echo.danger("Redirect from bucket should be empty.")
            raise Exception("Redirect from bucket is not empty.")

    def _create_bucket(self, bucket_name, region):
        if region == "us-east-1":
            self.s3_client.create_bucket(Bucket=bucket_name)
        else:
            self.s3_client.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": region},
            )

    def _ensure_redirect_from(self):
        self._ensure_redirect_from_exists()
        self._ensure_redirect_from_empty()
        self._ensure_redirect_from_website()

    def _ensure_redirect_from_website(self):
        for redirect_from in self.context.redirect_from:
            self.s3_client.put_bucket_website(
                Bucket=redirect_from.domain,
                WebsiteConfiguration={
                    "RedirectAllRequestsTo": {
                        "HostName": self.context.domain,
                    }
                },
            )

    def _ensure_redirect_from_exists(self):
        for redirect_from in self.context.redirect_from:
            if not self._bucket_exists(redirect_from.domain):
                self._create_bucket(redirect_from.domain, self.context.region)

    def _ensure_redirect_from_empty(self):
        for redirect_from in self.context.redirect_from:
            self._bucket_assert_empty(redirect_from.domain)

    def _delete_redirect_from(self):
        for redirect_from in self.context.redirect_from:
            bucket = redirect_from.domain
            try:
                if self._bucket_exists(bucket):
                    self._bucket_assert_empty(bucket)
                    self.s3_client.delete_bucket_website(Bucket=bucket)
                    echo.info("Bucket website hosting for %s was deleted." % bucket)
                    echo.warning("Delete the bucket manually: %s" % bucket)
                else:
                    echo.warning("Redirect from bucket does not exists.")
            except BucketOwnershipException:
                echo.danger("Redirect bucket might be already used by other account.")

    def _delete_redirect_from_policies(self, bucket_name):
        echo.danger("Delete redirect from bucket policies is implementing ...")
        echo.warning("Please delete the bucket policy manually.")
        echo.info("The bucket name: " + bucket_name)

    def _create_origin_bucket(self):
        self._create_bucket(self.context.bucket_name, self.context.region)

    def _origin_s3_exists(self):
        return self._bucket_exists(self.context.bucket_name)

    def _update_origin_bucket_policy(self, policy: dict | None):
        if policy is None:
            echo.info("Deleting bucket policy for %s." % self.context.bucket_name)
        else:
            echo.info("Updating bucket policy for %s." % self.context.bucket_name)
        echo.log("Current policy: %s" % self.bucket_policy)
        if policy is None:
            self.s3_client.delete_bucket_policy(Bucket=self.context.bucket_name)
        else:
            self.s3_client.put_bucket_policy(
                Bucket=self.context.bucket_name,
                Policy=json.dumps(policy),
            )
        echo.log("Updated policy: %s" % policy)
        del self.bucket_policy

    def _ensure_bucket_policy(self):
        if self.bucket_policy is None:
            self._update_origin_bucket_policy(
                {
                    "Version": self.bucket_policy_version_should_be,
                    "Statement": [self.bucket_policy_statement_should_contain],
                }
            )
        elif self.bucket_policy["Version"] != self.bucket_policy_version_should_be:
            raise Exception(
                "Bucket policy version is not supported. "
                "Please update the policy manually."
            )
        elif self.bucket_policy_require_update:
            bucket_policy_should_be = self.bucket_policy.copy()
            bucket_policy_should_be["Statement"].append(
                self.bucket_policy_statement_should_contain
            )
            self._update_origin_bucket_policy(bucket_policy_should_be)
        else:
            echo.info("Bucket policy is already configured properly.")

    def _delete_bucket_policy(self):
        delete_target = self.bucket_policy_statement_should_contain
        if self.bucket_policy is None:
            echo.info("Bucket policy is already None.")
        elif self.bucket_policy["Version"] != self.bucket_policy_version_should_be:
            echo.warning("Bucket policy version missmatch. Check the policy manually.")
        elif delete_target not in self.bucket_policy["Statement"]:
            echo.warning("No policy found. Check the policy manually.")
        else:
            bucket_policy_should_be = self.bucket_policy.copy()
            bucket_policy_should_be["Statement"].remove(delete_target)
            if not bucket_policy_should_be["Statement"]:
                self._update_origin_bucket_policy(None)
            else:
                self._update_origin_bucket_policy(bucket_policy_should_be)

    @property
    def bucket_policy_require_update(self):
        return (self.bucket_policy is None) or (
            self.bucket_policy_statement_should_contain
            not in self.bucket_policy["Statement"]
        )

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
    def bucket_policy_version_should_be(self):
        return "2012-10-17"

    @property
    def bucket_policy_statement_should_contain(self):
        return {
            "Sid": "AllowCloudFrontReadOnly%s" % self.context.origin_path,
            "Effect": "Allow",
            "Principal": {"Service": "cloudfront.amazonaws.com"},
            "Action": "s3:GetObject",
            "Resource": "arn:aws:s3:::%s%s/*"
            % (self.context.bucket_name, self.context.origin_path),
            "Condition": {
                "StringEquals": {
                    "AWS:SourceArn": "arn:aws:cloudfront::%s:distribution/%s"
                    % (self.account_id, self.distribution_id)
                }
            },
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
