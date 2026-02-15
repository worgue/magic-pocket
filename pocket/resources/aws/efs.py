from __future__ import annotations

import time
import uuid
from functools import cached_property
from typing import TYPE_CHECKING

import boto3

from ..base import ResourceStatus

if TYPE_CHECKING:
    from ...general_context import EfsContext


class Efs:
    context: EfsContext

    def __init__(self, context: EfsContext) -> None:
        self.context = context
        self.client = boto3.client("efs", region_name=context.region)

    @cached_property
    def description(self):
        for fs in self._iter_all_efs():
            for tag in fs["Tags"]:
                if tag["Key"] == "Name" and tag["Value"] == self.context.name:
                    return fs
        return None

    @cached_property
    def lifecycle_policies(self):
        res = self.client.describe_lifecycle_configuration(
            FileSystemId=self.filesystem_id
        )
        return res["LifecyclePolicies"]

    @property
    def lifecycle_policies_should_be(self):
        return [
            {
                "TransitionToIA": "AFTER_30_DAYS",
            },
            {
                "TransitionToPrimaryStorageClass": "AFTER_1_ACCESS",
            },
        ]

    @property
    def filesystem_id(self):
        if self.description:
            return self.description["FileSystemId"]

    def clear_status(self):
        if hasattr(self, "description"):
            del self.description
        if hasattr(self, "lifecycle_policies"):
            del self.lifecycle_policies

    def wait_status(self, status: ResourceStatus, timeout=60):
        max_iter = 100
        interval = 3
        if (timeout < 0) or ((max_iter * interval) < timeout):
            raise Exception("timeout value is out of range")
        for i in range(max_iter):
            self.clear_status()
            if self.status == status:
                print("")
                return
            if i == 0:
                print("Waiting for efs status to be %s" % status, end="", flush=True)
            print(".", end="", flush=True)
            time.sleep(interval)

    def create(self):
        res = self.client.create_file_system(
            CreationToken=str(uuid.uuid4()),
            Encrypted=True,
            Backup=True,
            Tags=[
                {
                    "Key": "Name",
                    "Value": self.context.name,
                },
            ],
        )
        filesystem_id = res["FileSystemId"]
        print(res)
        print(filesystem_id)
        self.wait_status("REQUIRE_UPDATE")
        self.ensure_lifecycle_policies()

    def update(self):
        self.client.put_lifecycle_configuration(
            FileSystemId=self.filesystem_id,
            LifecyclePolicies=self.lifecycle_policies_should_be,
        )

    def ensure_lifecycle_policies(self):
        if self.lifecycle_policies != self.lifecycle_policies_should_be:
            self.update()

    def delete(self):
        self.client.delete_file_system(FileSystemId=self.filesystem_id)

    def _iter_all_efs(self):
        res = self.client.describe_file_systems()
        if res.get("NextMarker"):
            raise Exception("Your efs number is over 100. Please implement here.")
        return res["FileSystems"]

    def exists(self):
        if self.description:
            return True
        return False

    def ensure_exists(self):
        if self.exists():
            return
        self.create()

    @property
    def status(self) -> ResourceStatus:
        if not self.exists():
            return "NOEXIST"
        assert self.description
        if self.description["LifeCycleState"] != "available":
            return "PROGRESS"
        if self.lifecycle_policies != [
            {
                "TransitionToIA": "AFTER_30_DAYS",
            },
            {
                "TransitionToPrimaryStorageClass": "AFTER_1_ACCESS",
            },
        ]:
            return "REQUIRE_UPDATE"
        return "COMPLETED"
