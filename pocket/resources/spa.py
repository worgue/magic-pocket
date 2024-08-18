from __future__ import annotations

from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from .base import ResourceStatus

if TYPE_CHECKING:
    from pocket.context import SpaContext


class Spa:
    context: SpaContext

    def __init__(self, context: SpaContext) -> None:
        self.context = context
        self.s3_client = boto3.client("s3", region_name=context.region)

    @property
    def status(self) -> ResourceStatus:
        if self._s3_exists():
            return "COMPLETED"
        return "NOEXIST"

    def create(self):
        if not self._s3_exists():
            self._create_s3_bucket()

    def _create_s3_bucket(self):
        self.s3_client.create_bucket(
            Bucket=self.context.domain,
            CreateBucketConfiguration={
                "LocationConstraint": self.context.region,
            },
        )

    def _s3_exists(self):
        try:
            self.s3_client.head_bucket(Bucket=self.context.domain)
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "404":
                return False
        raise Exception(
            "Bucket might be already used by other account. "
            "You may need to change the domain."
        )
