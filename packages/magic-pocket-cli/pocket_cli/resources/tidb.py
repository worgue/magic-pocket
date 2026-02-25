from __future__ import annotations

import json
import logging
import os
import secrets
import ssl
import string
import time
from functools import cached_property
from typing import TYPE_CHECKING

import requests
from pydantic import BaseModel
from requests.auth import HTTPDigestAuth

from pocket.resources.base import ResourceStatus

if TYPE_CHECKING:
    from pocket.context import TiDbContext

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(level=os.getenv("POCKET_LOGGER_LEVEL", "WARNING").upper())


class TiDbResourceIsNotReady(Exception):
    pass


class Project(BaseModel):
    id: str
    name: str


class Cluster(BaseModel):
    id: str
    name: str
    status: str
    host: str
    port: int
    user: str


class TiDbApi:
    serverless_endpoint = "https://serverless.tidbapi.com/v1beta1/"
    iam_endpoint = "https://iam.tidbapi.com/v1beta1/"

    def __init__(self, public_key: str, private_key: str) -> None:
        self.auth = HTTPDigestAuth(public_key, private_key)

    def _request(self, method: str, url: str, data=None):
        logger.info("%s %s" % (method, url))
        if data:
            logger.debug(json.dumps(data, indent=2))
        res = requests.request(method, url, auth=self.auth, json=data)
        logger.debug(res.status_code)
        logger.debug(json.dumps(res.json(), indent=2))
        if 200 <= res.status_code < 300:
            if method != "GET":
                time.sleep(2)
            return res
        raise RuntimeError("%s: %s" % (res.status_code, res.text))

    def iam_get(self, path: str):
        return self._request("GET", self.iam_endpoint + path)

    def serverless_get(self, path: str):
        return self._request("GET", self.serverless_endpoint + path)

    def serverless_post(self, path: str, data=None):
        return self._request("POST", self.serverless_endpoint + path, data)

    def serverless_put(self, path: str, data=None):
        return self._request("PUT", self.serverless_endpoint + path, data)

    def serverless_delete(self, path: str):
        return self._request("DELETE", self.serverless_endpoint + path)

    def list_projects(self) -> list[dict]:
        return self.iam_get("projects").json().get("projects", [])

    def list_clusters(self, project_id: str) -> list[dict]:
        return (
            self.serverless_get("clusters?filter=projectId=%s" % project_id)
            .json()
            .get("clusters", [])
        )

    def create_cluster(self, data: dict) -> dict:
        return self.serverless_post("clusters", data).json()

    def get_cluster(self, cluster_id: str) -> dict:
        return self.serverless_get("clusters/%s" % cluster_id).json()

    def delete_cluster(self, cluster_id: str):
        return self.serverless_delete("clusters/%s" % cluster_id)

    def change_password(self, cluster_id: str, password: str):
        return self.serverless_put(
            "clusters/%s/password" % cluster_id,
            {"password": password},
        )


class TiDb:
    context: TiDbContext
    _root_password: str | None

    def __init__(self, context: TiDbContext) -> None:
        self.context = context
        self._root_password = None

    @cached_property
    def api(self) -> TiDbApi:
        if not self.context.public_key or not self.context.private_key:
            raise TiDbResourceIsNotReady(
                "TiDB API keys not configured. "
                "Set tidb_public_key and tidb_private_key in .env"
            )
        return TiDbApi(self.context.public_key, self.context.private_key)

    @cached_property
    def project(self) -> Project | None:
        projects = self.api.list_projects()
        if self.context.tidb_project:
            for p in projects:
                if p["name"] == self.context.tidb_project:
                    return Project(id=str(p["id"]), name=p["name"])
            return None
        if len(projects) == 1:
            p = projects[0]
            return Project(id=str(p["id"]), name=p["name"])
        if len(projects) > 1:
            names = [p["name"] for p in projects]
            raise TiDbResourceIsNotReady(
                "複数の TiDB Cloud プロジェクトが見つかりました: %s\n"
                "pocket.toml の [tidb] で project を指定してください。\n"
                '例: project = "%s"' % (names, names[0])
            )
        return None

    @cached_property
    def cluster(self) -> Cluster | None:
        if not self.project:
            return None
        for c in self.api.list_clusters(self.project.id):
            if c.get("displayName") == self.context.cluster_name:
                endpoints = c.get("endpoints", {}).get("public", {})
                user_prefix = c.get("userPrefix", "")
                return Cluster(
                    id=str(c["clusterId"]),
                    name=c["displayName"],
                    status=c.get("state", "UNKNOWN"),
                    host=endpoints.get("host", ""),
                    port=int(endpoints.get("port", 4000)),
                    user="%s.root" % user_prefix,
                )
        return None

    @property
    def status(self) -> ResourceStatus:
        if not self.context.public_key or not self.context.private_key:
            return "NOEXIST"
        if self.cluster and self.cluster.status == "ACTIVE":
            return "COMPLETED"
        return "NOEXIST"

    @property
    def description(self) -> str:
        return "Create TiDB cluster and database"

    @property
    def database_url(self) -> str:
        if not self.cluster:
            raise TiDbResourceIsNotReady("Cluster not found")
        if self.cluster.status != "ACTIVE":
            raise TiDbResourceIsNotReady("Cluster is not available")
        password = self._root_password
        if not password:
            password = self._reset_password()
        return "mysql://%s:%s@%s:%d/%s" % (
            self.cluster.user,
            password,
            self.cluster.host,
            self.cluster.port,
            self.context.database_name,
        )

    def _generate_password(self) -> str:
        chars = string.ascii_letters + string.digits
        return "".join(secrets.choice(chars) for _ in range(24))

    def _reset_password(self) -> str:
        if not self.cluster:
            raise TiDbResourceIsNotReady("Cluster not found")
        new_password = self._generate_password()
        self.api.change_password(self.cluster.id, new_password)
        self._root_password = new_password
        return new_password

    def deploy_init(self):
        pass

    def create(self):
        self._ensure_cluster()
        self._ensure_database()

    def _ensure_cluster(self):
        if self.cluster:
            return
        if not self.project:
            raise TiDbResourceIsNotReady(
                "No TiDB Cloud project found. Create one at https://tidbcloud.com/"
            )
        password = self._generate_password()
        data = {
            "displayName": self.context.cluster_name,
            "region": {"name": "regions/aws-%s" % self.context.region},
            "rootPassword": password,
            "labels": {"tidb.cloud/project": self.project.id},
            "endpoints": {
                "public": {
                    "authorizedNetworks": [
                        {
                            "startIpAddress": "0.0.0.0",
                            "endIpAddress": "255.255.255.255",
                            "displayName": "Allow all",
                        }
                    ]
                }
            },
        }
        self.api.create_cluster(data)
        self._root_password = password
        self._wait_for_cluster()

    def _wait_for_cluster(self):
        logger.info("Waiting for cluster to become available...")
        del self.cluster
        for _ in range(60):
            if self.cluster and self.cluster.status == "ACTIVE":
                return
            time.sleep(5)
            del self.cluster
        raise TiDbResourceIsNotReady("Cluster did not become available in time")

    def _get_sql_connection(self):
        try:
            import pymysql  # noqa: PLC0415
        except ModuleNotFoundError:
            raise ModuleNotFoundError(
                "pymysql is required for TiDB database management.\n"
                "Install it with: uv add pymysql"
            ) from None
        if not self.cluster:
            raise TiDbResourceIsNotReady("Cluster not found")
        password = self._root_password
        if not password:
            password = self._reset_password()
        ssl_ctx = ssl.create_default_context()
        return pymysql.connect(
            host=self.cluster.host,
            port=self.cluster.port,
            user=self.cluster.user,
            password=password,
            ssl=ssl_ctx,
        )

    def _ensure_database(self):
        conn = self._get_sql_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "CREATE DATABASE IF NOT EXISTS `%s`" % self.context.database_name
                )
        finally:
            conn.close()

    def delete_cluster(self):
        if not self.cluster:
            raise TiDbResourceIsNotReady("Cluster not found")
        self.api.delete_cluster(self.cluster.id)
        del self.cluster

    def reset_database(self):
        conn = self._get_sql_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "DROP DATABASE IF EXISTS `%s`" % self.context.database_name
                )
                cursor.execute("CREATE DATABASE `%s`" % self.context.database_name)
        finally:
            conn.close()
