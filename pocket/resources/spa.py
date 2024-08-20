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
        self.stack.wait_status("COMPLETED")
        self._ensure_bucket_policy()
        self._ensure_origin_access_control()
        self._ensure_distribution_use_oac()

    def delete(self):
        self._remove_oac_from_distribution()
        self._delete_origin_access_control()
        self._delete_bucket_policy()
        self.stack.delete()
        echo.info("Deleting cloudformation stack for spa ...")
        echo.warning("Please delete the bucket resources manually.")
        echo.warning("The bucket name: " + self.context.bucket_name)

    def _update_oac_id(self, value):
        get_res = self.cf_client.get_distribution_config(Id=self.distribution_id)
        data = get_res["DistributionConfig"].copy()
        for item in data["Origins"]["Items"]:
            if item["Id"] == self.context.origin_id:
                if item["OriginAccessControlId"] == value:
                    echo.log("OriginAccessControlId is already set.")
                    return
                item["OriginAccessControlId"] = value
        self.cf_client.update_distribution(
            Id=self.distribution_id,
            DistributionConfig=data,
            IfMatch=get_res["ETag"],
        )

    def _ensure_distribution_use_oac(self):
        self._update_oac_id(self.origin_access_control.Id)

    def _remove_oac_from_distribution(self):
        self._update_oac_id("")

    @cached_property
    def origin_access_control(self) -> OriginAccessControl:
        res = self.cf_client.list_origin_access_controls(MaxItems="100")
        items = res["OriginAccessControlList"].get("Items", [])
        if 100 <= len(items):
            raise Exception("Pagination is not supported yet.")
        for item in items:
            if item["Name"] == self.context.oac_config_name:
                return OriginAccessControl(**item)
        raise NoOacException("OriginAccessControlConfig not found.")

    def _has_origin_access_control(self):
        try:
            return bool(self.origin_access_control)
        except NoOacException:
            return False

    def _ensure_origin_access_control(self):
        # 以下のドキュメントによれば、S3のパーミンション追加の後に作成する必要がある
        # また、cloudformationで作ることはできても、
        # cloudformationでdistributionに紐づける方法が見つからなかった
        # https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-s3.html
        # 暫定手動対応
        if self._has_origin_access_control():
            print("OriginAccessControlConfig already exists.")
        else:
            print("Create OriginAccessControlConfig")
            self._create_origin_access_control()

    def _delete_origin_access_control(self):
        if self._has_origin_access_control():
            res = self.cf_client.get_origin_access_control(
                Id=self.origin_access_control.Id
            )
            self.cf_client.delete_origin_access_control(
                Id=self.origin_access_control.Id,
                IfMatch=res["ETag"],
            )

    def _create_origin_access_control(self):
        self.cf_client.create_origin_access_control(
            OriginAccessControlConfig={
                "Name": self.context.oac_config_name,
                "OriginAccessControlOriginType": "s3",
                "SigningProtocol": "sigv4",
                "SigningBehavior": "always",
            }
        )

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
