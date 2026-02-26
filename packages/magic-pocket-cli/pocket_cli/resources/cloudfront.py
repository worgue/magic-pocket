from __future__ import annotations

import json
import mimetypes
import subprocess
import time
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel

from pocket.resources.base import ResourceStatus
from pocket.utils import echo
from pocket_cli.resources.aws.cloudformation import CloudFrontStack
from pocket_cli.resources.aws.s3_utils import bucket_exists, create_bucket

if TYPE_CHECKING:
    from pocket.context import CloudFrontContext, RouteContext


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


class CloudFront:
    context: CloudFrontContext

    def __init__(self, context: CloudFrontContext) -> None:
        self.context = context
        self.s3_client = boto3.client("s3", region_name=context.region)
        self.cf_client = boto3.client("cloudfront", region_name=context.region)

    @property
    def description(self):
        return (
            "Create cloudformation(for cloudfront) using s3 bucket: %s"
            % self.context.bucket_name
        )

    def state_info(self):
        key = "cloudfront-%s" % self.context.name
        return {key: {"bucket_name": self.context.bucket_name}}

    def deploy_init(self):
        self.warn_contents()

    @property
    def status(self) -> ResourceStatus:
        return self.stack.status

    @property
    def stack(self):
        return CloudFrontStack(self.context)

    def create(self):
        self.update()

    def update(self):
        self._ensure_redirect_from()
        if not self.stack.exists:
            self.stack.create()
        elif not self.stack.yaml_synced:
            self.stack.update()
        info = echo.info
        log = echo.log
        log("Waiting for cloudformation stack to be completed ...")
        log("This may take a few minutes.")
        log("Because cloudfront distribution id is required to set s3 bucket policy.")
        info("If you want to exit, you can safely kill this process.")
        info("In that case, run `pocket resource cloudfront update` later.")
        self.stack.wait_status("COMPLETED", timeout=600, interval=10)
        self._ensure_bucket_policy()
        log("Bucket for cloudfront is ready.")
        self.warn_contents()

    def upload(self, *, skip_build: bool = False):
        for route in self.context.uploadable_routes:
            if route.build and not skip_build:
                echo.info("ビルド実行: %s" % route.build)
                subprocess.run(route.build, shell=True, check=True)
            self._upload_route(route)
        if self.context.uploadable_routes:
            self._invalidate()

    def _upload_route(self, route: RouteContext):
        s3_prefix = (
            self.context.origin_prefix + route.path_pattern.rstrip("/*")
        ).lstrip("/")
        assert route.build_dir
        local_dir = Path(route.build_dir)
        uploaded_keys: set[str] = set()
        for file in local_dir.rglob("*"):
            if file.is_dir():
                continue
            relative = file.relative_to(local_dir)
            s3_key = s3_prefix + "/" + str(relative)
            uploaded_keys.add(s3_key)
            extra_args: dict[str, str] = {
                "ContentType": mimetypes.guess_type(str(file))[0]
                or "application/octet-stream"
            }
            if route.is_spa:
                if file.suffix in (".html", ".htm"):
                    extra_args["CacheControl"] = "no-cache, no-store"
                else:
                    extra_args["CacheControl"] = "max-age=31536000"
            self.s3_client.upload_file(
                str(file),
                self.context.bucket_name,
                s3_key,
                ExtraArgs=extra_args,
            )
            echo.log("アップロード: s3://%s/%s" % (self.context.bucket_name, s3_key))
        self._delete_stale_objects(s3_prefix, uploaded_keys)
        echo.info(
            "%d ファイルをアップロードしました (prefix: %s)"
            % (len(uploaded_keys), s3_prefix)
        )

    def _delete_stale_objects(self, prefix: str, uploaded_keys: set[str]):
        paginator = self.s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=self.context.bucket_name, Prefix=prefix + "/"
        ):
            for obj in page.get("Contents", []):
                if obj["Key"] not in uploaded_keys:
                    self.s3_client.delete_object(
                        Bucket=self.context.bucket_name, Key=obj["Key"]
                    )
                    echo.log(
                        "削除: s3://%s/%s" % (self.context.bucket_name, obj["Key"])
                    )

    def _invalidate(self):
        self.cf_client.create_invalidation(
            DistributionId=self.distribution_id,
            InvalidationBatch={
                "Paths": {"Quantity": 1, "Items": ["/*"]},
                "CallerReference": str(int(time.time())),
            },
        )
        echo.info("CloudFront キャッシュ無効化をリクエストしました")

    def warn_contents(self):
        bucket = self.context.bucket_name
        for route in self.context.routes:
            if route.build_dir:
                continue
            origin = self.context.origin_prefix + route.path_pattern
            echo.warning("Upload files manually to s3://%s%s" % (bucket, origin))
            if route.is_spa:
                echo.info("%s is a spa route." % (route.path_pattern or "default"))
                echo.info("Set proper cahce headers.")
                eg_cmd = "npx s3-spa-upload build %s --delete --prefix %s" % (
                    bucket,
                    origin[1:],
                )
                echo.info("e.g) " + eg_cmd)
            elif route.is_versioned:
                echo.info("This is a versioned route.")
                echo.info(
                    "Just upload your files. CloudFront will set cache-control headers."
                )
                eg_cmd = "aws s3 sync data s3://%s%s" % (bucket, origin)
                echo.info("e.g) " + eg_cmd)

    def delete(self):
        self._delete_redirect_from()
        self._delete_bucket_policy()
        self.stack.delete()
        echo.info("Deleting cloudformation stack for cloudfront ...")
        echo.warning(
            "S3 bucket is managed by the S3 resource: " + self.context.bucket_name
        )

    def _bucket_exists(self, bucket_name):
        try:
            return bucket_exists(self.s3_client, bucket_name)
        except ClientError as e:
            raise BucketOwnershipException(
                "Bucket might be already used by other account. "
                "You may need to change the domain."
            ) from e

    def _bucket_assert_empty(self, bucket_name):
        res = self.s3_client.list_objects_v2(Bucket=bucket_name)
        if "Contents" in res:
            echo.danger("Redirect from bucket should be empty.")
            raise Exception("Redirect from bucket is not empty.")

    def _create_bucket(self, bucket_name, region):
        create_bucket(self.s3_client, bucket_name, region)

    def _ensure_redirect_from(self):
        self._ensure_redirect_from_exists()
        self._ensure_redirect_from_empty()
        self._ensure_redirect_from_website()

    def _ensure_redirect_from_website(self):
        if not self.context.redirect_from:
            return
        assert self.context.domain, "domain is required when redirect_from is set"
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
            echo.warning(
                "Bucket policy version %s will be upgraded to %s."
                % (self.bucket_policy["Version"], self.bucket_policy_version_should_be)
            )
            bucket_policy_should_be = self.bucket_policy.copy()
            bucket_policy_should_be["Version"] = self.bucket_policy_version_should_be
            if (
                self.bucket_policy_statement_should_contain
                not in bucket_policy_should_be["Statement"]
            ):
                bucket_policy_should_be["Statement"].append(
                    self.bucket_policy_statement_should_contain
                )
            self._update_origin_bucket_policy(bucket_policy_should_be)
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
            "Sid": "AllowCloudFrontReadOnly%s" % self.context.yaml_key,
            "Effect": "Allow",
            "Principal": {"Service": "cloudfront.amazonaws.com"},
            "Action": "s3:GetObject",
            "Resource": "arn:aws:s3:::%s%s/*"
            % (self.context.bucket_name, self.context.origin_prefix),
            "Condition": {
                "StringEquals": {
                    "AWS:SourceArn": "arn:aws:cloudfront::%s:distribution/%s"
                    % (self.account_id, self.distribution_id)
                }
            },
        }
