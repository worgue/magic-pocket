from __future__ import annotations

import time
from functools import cached_property
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from pocket.resources.base import ResourceStatus
from pocket.utils import echo

if TYPE_CHECKING:
    from pocket.context import DsqlContext


class Dsql:
    context: DsqlContext

    def __init__(self, context: DsqlContext) -> None:
        self.context = context
        self._client = boto3.client("dsql", region_name=context.region)

    @cached_property
    def cluster(self) -> dict | None:
        """Name タグで DSQL クラスターを検索"""
        paginator = self._client.get_paginator("list_clusters")
        for page in paginator.paginate():
            for cluster in page["clusters"]:
                identifier = cluster["identifier"]
                try:
                    detail = self._client.get_cluster(Identifier=identifier)
                    tags = self._client.list_tags_for_resource(
                        ResourceArn=detail["arn"]
                    )
                    if tags.get("tags", {}).get("Name") == self.context.tag_name:
                        return detail
                except ClientError:
                    continue
        return None

    @property
    def identifier(self) -> str | None:
        if self.cluster:
            return self.cluster["identifier"]
        return None

    @property
    def endpoint(self) -> str | None:
        if self.identifier:
            return f"{self.identifier}.dsql.{self.context.region}.on.aws"
        return None

    @property
    def arn(self) -> str | None:
        if self.cluster:
            return self.cluster["arn"]
        return None

    @property
    def status(self) -> ResourceStatus:
        if self.cluster is None:
            return "NOEXIST"
        cluster_status = self.cluster["status"]
        if cluster_status in ("CREATING", "UPDATING", "DELETING"):
            return "PROGRESS"
        if cluster_status == "ACTIVE":
            return "COMPLETED"
        return "FAILED"

    @property
    def description(self):
        return "Create Aurora DSQL cluster: %s" % self.context.tag_name

    def state_info(self):
        return {
            "dsql": {
                "tag_name": self.context.tag_name,
                "identifier": self.identifier,
                "endpoint": self.endpoint,
            }
        }

    def deploy_init(self):
        pass

    def create(self):
        echo.log("Creating DSQL cluster: %s" % self.context.tag_name)
        res = self._client.create_cluster(
            deletionProtectionEnabled=self.context.deletion_protection,
            tags={"Name": self.context.tag_name},
        )
        identifier = res["identifier"]
        echo.log("Cluster ID: %s" % identifier)
        echo.log("Waiting for DSQL cluster to become active...")
        self._wait_active(identifier, timeout=600)
        self.clear_cache()
        echo.success("DSQL cluster is now active.")
        echo.success("Endpoint: %s" % self.endpoint)

    def delete(self):
        if not self.identifier:
            return
        echo.log("Deleting DSQL cluster: %s" % self.identifier)
        self._client.delete_cluster(Identifier=self.identifier)
        echo.log("Waiting for DSQL cluster deletion...")
        self._wait_deleted(self.identifier, timeout=600)
        echo.success("DSQL cluster was deleted.")

    def _wait_active(self, identifier: str, timeout: int = 600, interval: int = 5):
        for i in range(timeout // interval):
            try:
                res = self._client.get_cluster(Identifier=identifier)
                if res["status"] == "ACTIVE":
                    print("")
                    return
            except ClientError:
                pass
            if i == 0:
                print("Waiting for cluster to be active", end="", flush=True)
            print(".", end="", flush=True)
            time.sleep(interval)
        raise TimeoutError("Cluster did not become active within %s seconds" % timeout)

    def _wait_deleted(self, identifier: str, timeout: int = 600, interval: int = 5):
        for i in range(timeout // interval):
            try:
                self._client.get_cluster(Identifier=identifier)
            except ClientError as e:
                if e.response["Error"]["Code"] == "ResourceNotFoundException":
                    print("")
                    return
                raise
            if i == 0:
                print("Waiting for cluster deletion", end="", flush=True)
            print(".", end="", flush=True)
            time.sleep(interval)
        raise TimeoutError("Cluster not deleted within %s seconds" % timeout)

    def clear_cache(self):
        if "cluster" in self.__dict__:
            del self.__dict__["cluster"]
