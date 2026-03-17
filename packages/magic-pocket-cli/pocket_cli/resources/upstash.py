from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING

import requests
from pydantic import BaseModel

from pocket.resources.base import ResourceStatus
from pocket.utils import echo

if TYPE_CHECKING:
    from pocket.context import UpstashContext


class UpstashResourceIsNotReady(Exception):
    pass


class UpstashDatabase(BaseModel):
    database_id: str
    database_name: str
    endpoint: str
    port: int
    password: str
    tls: bool
    state: str


class UpstashApi:
    base_url = "https://api.upstash.com/v2/redis"

    def __init__(self, email: str, api_key: str) -> None:
        self.auth = (email, api_key)

    def get(self, path: str) -> requests.Response:
        res = requests.get(f"{self.base_url}/{path}", auth=self.auth)
        if 200 <= res.status_code < 300:
            return res
        raise RuntimeError("Upstash API error: %s %s" % (res.status_code, res.text))

    def post(self, path: str, data: dict) -> requests.Response:
        res = requests.post(f"{self.base_url}/{path}", auth=self.auth, json=data)
        if 200 <= res.status_code < 300:
            return res
        raise RuntimeError("Upstash API error: %s %s" % (res.status_code, res.text))

    def delete(self, path: str) -> requests.Response:
        res = requests.delete(f"{self.base_url}/{path}", auth=self.auth)
        if 200 <= res.status_code < 300:
            return res
        raise RuntimeError("Upstash API error: %s %s" % (res.status_code, res.text))


class Upstash:
    context: UpstashContext

    def __init__(self, context: UpstashContext) -> None:
        self.context = context

    @property
    def api(self) -> UpstashApi:
        if not self.context.email or not self.context.api_key:
            raise UpstashResourceIsNotReady(
                "UPSTASH_EMAIL と UPSTASH_API_KEY を設定してください"
            )
        return UpstashApi(self.context.email, self.context.api_key)

    @cached_property
    def database(self) -> UpstashDatabase | None:
        res = self.api.get("databases")
        for db in res.json():
            if db["database_name"] == self.context.database_name:
                return UpstashDatabase(
                    **{k: db[k] for k in UpstashDatabase.model_fields if k in db}
                )
        return None

    @property
    def redis_url(self) -> str:
        if self.database is None:
            raise UpstashResourceIsNotReady("データベースが見つかりません")
        if self.database.state != "active":
            raise UpstashResourceIsNotReady(
                "データベースが active ではありません: %s" % self.database.state
            )
        protocol = "rediss" if self.database.tls else "redis"
        return "%s://default:%s@%s:%d" % (
            protocol,
            self.database.password,
            self.database.endpoint,
            self.database.port,
        )

    @property
    def status(self) -> ResourceStatus:
        if self.database and self.database.state == "active":
            return "COMPLETED"
        return "NOEXIST"

    @property
    def description(self):
        return "Create Upstash Redis database: %s" % self.context.database_name

    def state_info(self):
        return {
            "upstash": {
                "database_name": self.context.database_name,
            }
        }

    def deploy_init(self):
        pass

    def create(self):
        if self.database is not None:
            echo.info(
                "Upstash データベース '%s' は既に存在します"
                % self.context.database_name
            )
            return
        echo.log("Upstash データベースを作成します: %s" % self.context.database_name)
        self.api.post(
            "database",
            {
                "database_name": self.context.database_name,
                "platform": "aws",
                "primary_region": "ap-southeast-1",
                "plan": "payg",
                "budget": self.context.budget,
                "tls": True,
                "eviction": True,
            },
        )
        if hasattr(self, "database"):
            del self.database
        echo.success(
            "Upstash データベースを作成しました: %s" % self.context.database_name
        )

    def delete_database(self):
        if self.database is None:
            echo.warning("Upstash データベースが見つかりません")
            return
        echo.log("Upstash データベースを削除します: %s" % self.context.database_name)
        self.api.delete("database/%s" % self.database.database_id)
        if hasattr(self, "database"):
            del self.database
        echo.success("Upstash データベースを削除しました")
