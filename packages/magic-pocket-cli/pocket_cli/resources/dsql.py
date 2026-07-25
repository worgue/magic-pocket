from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from pocket.resources.base import ResourceStatus
from pocket.secret_store import (
    delete_stored_value,
    put_stored_value,
    read_stored_value,
)
from pocket.utils import echo
from pocket_cli.resources.aws.poll import wait_until

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
                    detail = self._client.get_cluster(identifier=identifier)
                    tags = self._client.list_tags_for_resource(
                        resourceArn=detail["arn"]
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
                "endpoint_secret_name": self.context.endpoint_secret_name,
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
        # ensure_post_deploy_state でも publish されるが、後続 resource の失敗で
        # hook まで到達しない deploy でも作成記録が残るよう、作成直後にも書く
        self.publish_endpoint()

    def delete(self):
        if not self.identifier:
            return
        echo.log("Deleting DSQL cluster: %s" % self.identifier)
        self._client.delete_cluster(identifier=self.identifier)
        echo.log("Waiting for DSQL cluster deletion...")
        self._wait_deleted(self.identifier, timeout=600)
        echo.success("DSQL cluster was deleted.")
        self._unpublish_endpoint()

    def ensure_post_deploy_state(self):
        # create の有無に関わらず毎 deploy で publish を冪等に確保する。cluster
        # 再作成や旧バージョンで deploy 済みの既存 cluster も常に実体を反映する
        self.publish_endpoint()

    def publish_endpoint(self):
        """endpoint を stored user secret 正準パスへ publish する (作成記録)。

        DSQL は cluster identifier が AWS 自動生成のため endpoint を naming から
        導出できない。deploy の外の消費者 (migration ツール等) が endpoint を
        決定的に引けるよう、作成者である deploy が正準パスへ書き込む。
        値が既に一致していれば書き込まない (SSM version / SM stage の増殖防止)。
        """
        name = self.context.endpoint_secret_name
        endpoint = self.endpoint
        if not name or endpoint is None:
            return
        store = self.context.endpoint_secret_store
        current = read_stored_value(name, store, self.context.region)
        if current == endpoint:
            return
        put_stored_value(name, store, endpoint, self.context.region)
        echo.log("Published DSQL endpoint to %s" % name)

    def _unpublish_endpoint(self):
        """publish 済み endpoint を削除する (cluster 削除との対称操作)。

        残すと削除済み cluster の endpoint を消費者が読み続けるため、削除まで
        deploy 側の責務とする。未 publish (NotFound) は握りつぶす。
        """
        name = self.context.endpoint_secret_name
        if not name:
            return
        delete_stored_value(
            name,
            self.context.endpoint_secret_store,
            self.context.region,
            force_sm=True,
            swallow_not_found=True,
        )
        echo.log("Removed published DSQL endpoint: %s" % name)

    def _wait_active(self, identifier: str, timeout: int = 600, interval: int = 5):
        def poll():
            try:
                res = self._client.get_cluster(identifier=identifier)
                return res["status"] == "ACTIVE"
            except ClientError:
                return False

        wait_until(
            poll,
            timeout=timeout,
            interval=interval,
            start_message="Waiting for cluster to be active",
            timeout_message=(
                "Cluster did not become active within %s seconds" % timeout
            ),
        )

    def _wait_deleted(self, identifier: str, timeout: int = 600, interval: int = 5):
        def poll():
            try:
                self._client.get_cluster(identifier=identifier)
                return False
            except ClientError as e:
                if e.response["Error"]["Code"] == "ResourceNotFoundException":
                    return True
                raise

        wait_until(
            poll,
            timeout=timeout,
            interval=interval,
            start_message="Waiting for cluster deletion",
            timeout_message="Cluster not deleted within %s seconds" % timeout,
        )

    def clear_cache(self):
        if "cluster" in self.__dict__:
            del self.__dict__["cluster"]
