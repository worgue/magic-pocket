"""Neon の ensure + 接続 URL 算出を import 可能な公開 API として提供する。

`pocket resource neon store-url` (CLI) が pocket.toml 前提で行う「branch 作成 →
role ensure + reveal_password → db ensure → endpoint host で URL 組み立て」を、
runtime package (`magic-pocket`) 側から pocket.toml なしで呼べるようにする。

外部 provisioner が backend 作成時に接続 URL を stored user secret へ焼く用途で
使う (SSM への保存自体は呼び出し側の責務。正準名は :func:`pocket.naming.
stored_user_secret_name` で導出できる)。CLI の `pocket_cli.resources.neon` は本
モジュールの re-export であり、実装はここに一本化されている。

HTTP は stdlib `urllib` で実装し、runtime package に新規依存を足さない
(requests は magic-pocket-cli 側の依存のまま)。
"""

from __future__ import annotations

import json
import logging
import re
import time
from functools import cached_property
from typing import Literal
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from pydantic import BaseModel

from pocket.context import NeonContext
from pocket.resources.base import ResourceStatus

# basicConfig / setLevel はライブラリ import の副作用として呼び出し側の
# root logger 設定を書き換えるため行わない (レベル設定は呼び出し側の責務)
logger = logging.getLogger(__name__)

ResourceType = Literal["branches", "databases", "endpoints", "roles"]


class NeonResourceIsNotReady(Exception):
    pass


class NeonNotFound(Exception):
    """Neon API が 404 を返したことを示す例外。

    `branch` / `role` などの個別取得で対象が存在しない場合にこの例外を投げる。
    呼び出し側は存在しないことを明示的に判定できる。
    """

    pass


class Project(BaseModel):
    id: str
    name: str


class Branch(BaseModel):
    id: str
    name: str
    # root branch (project 作成時の default) には parent_id が無い。root は Neon 仕様で
    # branch 単位の削除ができない (422: cannot delete the root branch) ため、削除経路の
    # 分岐 (destroy_plan) に使う。
    parent_id: str | None = None


class Database(BaseModel):
    name: str
    owner_name: str


class Endpoint(BaseModel):
    id: str
    host: str
    autoscaling_limit_min_cu: float
    autoscaling_limit_max_cu: float
    type: Literal["read_write", "read_only"]


class Role(BaseModel):
    name: str
    password: str | None = None


class _HttpResponse:
    """`requests.Response` のうち本モジュールが使う面 (status_code / json) だけを
    urllib で満たす最小ラッパ。"""

    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        return json.loads(self._body) if self._body else {}


def _http_request(
    method: str,
    url: str,
    *,
    headers: dict,
    data: dict | None = None,
    timeout: int = 30,
) -> _HttpResponse:
    """urllib で Neon API を叩く。4xx/5xx も例外にせず `_HttpResponse` で返し、
    status_code ベースの分岐 (404→NeonNotFound 等) を呼び出し側に委ねる。"""
    if not url.startswith("https://"):
        raise ValueError("Neon API URL must be https: %s" % url)
    req_headers = dict(headers)
    body: bytes | None = None
    if data is not None:
        body = json.dumps(data).encode()
        req_headers["Content-Type"] = "application/json"
    # https 固定は関数冒頭で検証済み (S310: 許可スキームの監査)
    req = Request(url, data=body, headers=req_headers, method=method)  # noqa: S310
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return _HttpResponse(resp.status, resp.read())
    except HTTPError as e:
        return _HttpResponse(e.code, e.read())


class NeonApi:
    endpoint = "https://console.neon.tech/api/v2/"

    def __init__(self, key) -> None:
        self.key = key

    @property
    def header(self):
        return {
            "Accept": "application/json",
            "Authorization": "Bearer %s" % self.key,
        }

    def _request(self, method: str, path: str, data=None) -> _HttpResponse:
        url = self.endpoint + path
        log = logger.info if method == "GET" else logger.warning
        log("%s %s" % (method, url))
        if data is not None:
            logger.debug(json.dumps(data, indent=2))
        res = _http_request(method, url, headers=self.header, data=data)
        logger.debug(res.status_code)
        if 200 <= res.status_code < 300:
            if method != "GET":
                # Neon 側の反映待ち (operations polling の簡易代替)
                time.sleep(2)
            return res
        detail = self._error_detail(res)
        if method == "GET" and res.status_code == 404:
            raise NeonNotFound("%s: %s" % (res.status_code, detail))
        if res.status_code == 401:
            self._print_auth_hint()
        raise Exception("%s: %s" % (res.status_code, detail))

    @staticmethod
    def _error_detail(res: _HttpResponse) -> str:
        """エラーレスポンスから安全にメッセージを取り出す。

        非 JSON / 空 body (LB 由来の 502 HTML 等) や message キー欠落で
        KeyError / JSONDecodeError になり本来の HTTP エラーを隠さないようにする。
        """
        try:
            payload = res.json()
        except json.JSONDecodeError:
            return "<non-JSON response>"
        if isinstance(payload, dict) and "message" in payload:
            return str(payload["message"])
        return json.dumps(payload)[:200]

    def _print_auth_hint(self):
        if not self.key:
            print("NEON_API_KEY が未設定です (Authorization: Bearer None)。")
            return
        # 診断用に prefix と長さのみ表示 (末尾まで出すと部分的な secret 露出になる)
        print("Used API key: %s..." % self.key[:5])
        print("API key length: %s" % len(self.key))

    def get(self, path):
        return self._request("GET", path)

    def post(self, path, data=None):
        return self._request("POST", path, data=data)

    def delete(self, path, data=None):
        return self._request("DELETE", path, data=data)

    def projects_url(self):
        return self.endpoint + "projects"


class Neon:
    context: NeonContext

    def __init__(self, context: NeonContext) -> None:
        self.context = context

    def get_resource_path(self, resource_type: ResourceType) -> str:
        requirements = {
            "branches": ["project"],
            "databases": ["project", "branch"],
            "endpoints": ["project"],
            "roles": ["project", "branch"],
        }
        path_templates = {
            "branches": "projects/%(project_id)s/branches",
            "databases": "projects/%(project_id)s/branches/%(branch_id)s/databases",
            "endpoints": "projects/%(project_id)s/endpoints",
            "roles": "projects/%(project_id)s/branches/%(branch_id)s/roles",
        }
        path_context = {}
        for requirement in requirements[resource_type]:
            if not getattr(self, requirement):
                raise Exception("%s not found" % requirement)
            path_context[requirement + "_id"] = getattr(self, requirement).id
        return path_templates[resource_type] % path_context

    def construct_path(
        self, resource_type: ResourceType, resource_id: str | None = None
    ):
        path = self.get_resource_path(resource_type)
        if resource_id:
            path += "/" + resource_id
        return path

    def get(self, resource_type: ResourceType, resource_id: str | None = None):
        path = self.construct_path(resource_type, resource_id)
        return self.api.get(path)

    def post(
        self, resource_type: ResourceType, resource_id: str | None = None, data=None
    ):
        path = self.construct_path(resource_type, resource_id)
        return self.api.post(path, data)

    def delete(
        self, resource_type: ResourceType, resource_id: str | None = None, data=None
    ):
        path = self.construct_path(resource_type, resource_id)
        return self.api.delete(path, data)

    @property
    def api(self):
        return NeonApi(self.context.api_key)

    @cached_property
    def role(self) -> Role | None:
        if not self.branch:
            return None
        try:
            res = self.get("roles", self.context.role_name)
        except NeonNotFound:
            return None
        return Role(**res.json()["role"])

    @cached_property
    def project(self) -> Project:
        """project_nameからNeonプロジェクトを解決する。

        組織キー: GET /projects で一覧取得し name で検索。
        プロジェクトスコープキー: エラーから project_id を取得し直接アクセス。
        """
        res = _http_request(
            "GET", self.api.endpoint + "projects", headers=self.api.header
        )
        if 200 <= res.status_code < 300:
            for p in res.json().get("projects", []):
                if p["name"] == self.context.project_name:
                    return Project(**p)
            raise ValueError(f"Neon project '{self.context.project_name}' not found")

        # プロジェクトスコープキー: エラーから project_id をパース
        message = res.json().get("message", "")
        match = re.search(r'subject_project_id:"([^"]+)"', message)
        if not match:
            raise ValueError(
                f"Failed to resolve Neon project: {res.status_code}: {message}"
            )
        project_id = match.group(1)
        project_res = self.api.get(f"projects/{project_id}")
        project_data = project_res.json()["project"]
        if project_data["name"] != self.context.project_name:
            raise ValueError(
                f"Neon project name mismatch: "
                f"config='{self.context.project_name}', "
                f"actual='{project_data['name']}'"
            )
        return Project(id=project_data["id"], name=project_data["name"])

    @cached_property
    def branch(self) -> Branch | None:
        if self.project:
            for branch in self.get("branches").json()["branches"]:
                if branch["name"] == self.context.branch_name:
                    return Branch(**branch)

    @cached_property
    def parent_branch(self) -> Branch | None:
        """branch を新規作成する際の親ブランチ。

        context.parent_branch_name が未指定なら None (= create_branch が parent_id を
        送らず Neon の default ブランチから分岐)。指定があるのに project 内に該当
        ブランチが無い場合はエラー (黙って default 分岐すると事故になるため)。
        """
        if not self.context.parent_branch_name:
            return None
        if self.project:
            for branch in self.get("branches").json()["branches"]:
                if branch["name"] == self.context.parent_branch_name:
                    return Branch(**branch)
        raise ValueError(
            f"Neon parent branch '{self.context.parent_branch_name}' not found "
            f"in project '{self.context.project_name}'"
        )

    @cached_property
    def database(self) -> Database | None:
        if self.branch:
            for database in self.get("databases").json()["databases"]:
                if database["name"] == self.context.name:
                    return Database(**database)

    @cached_property
    def endpoint(self) -> Endpoint | None:
        # read replica (read_only endpoint) を追加した branch では一覧の並びに
        # 依存せず read_write を選ぶ (read_only を返すと書き込みが全滅する)
        if self.branch:
            for endpoint in self.get("endpoints").json()["endpoints"]:
                if (
                    endpoint["branch_id"] == self.branch.id
                    and endpoint["type"] == "read_write"
                ):
                    return Endpoint(**endpoint)

    @property
    def database_url(self):
        if self.role is None or self.endpoint is None:
            raise NeonResourceIsNotReady("Create role and endpoint first")
        if self.role.password is None:
            self.set_role_password()
        # 解析側 (pocket.django.db_url) は unquote するため、生成側は必ず quote する
        # (runtime._set_rds_database_url と同じ対称性)
        return "postgres://%s:%s@%s:5432/%s?sslmode=require" % (
            quote(self.context.role_name, safe=""),
            quote(self.role.password or "", safe=""),
            self.endpoint.host,
            self.context.name,
        )

    @property
    def status(self) -> ResourceStatus:
        # provisioning="command" の Neon は get_resources で除外されるため、ここに
        # 到達するのは deploy が Neon を管理する provisioning="deploy" の場合のみ。
        if self.working:
            return "COMPLETED"
        return "NOEXIST"

    @property
    def working(self):
        check = [self.branch, self.database, self.endpoint, self.role]
        logger.info(str(check))
        return all(check)

    @property
    def description(self):
        return "Create Neon branch, database, role, and endpoint"

    def create_new(self):
        self.create()
        self.reset_database()

    def state_info(self):
        return {
            "neon": {
                "project_name": self.context.project_name,
                "branch_name": self.context.branch_name,
            }
        }

    def deploy_init(self):
        pass

    def create(self):
        # branch が既に存在するなら (Neon project 作成時に自動生成される default main
        # を含む) その上に role/database を ensure するだけにし branch 作成はスキップ。
        # parent_branch の解決も branch 不在時のみ行う (存在するのに親を要求して誤って
        # ValueError にしないため)。これで default main を使う stage の初回 deploy が
        # 409 (branch already exists) にならず、既存 branch への db/role bootstrap も
        # deploy で完結する。
        if self.branch is None:
            # parent_branch_name 指定時はその親から分岐。未指定なら parent_branch=None
            # で従来通り Neon default ブランチから分岐する。
            self.create_branch(self.parent_branch)
        self.ensure_role()
        self.ensure_database()

    def create_branch(self, base_branch: Branch | None = None):
        # 冪等化: context.branch_name の branch が既に存在するなら POST /branches は
        # 409 (branch already exists) になるため作成をスキップする。branch_out /
        # store_url の呼び出し側も branch 不在を確認済だが、直接呼ばれた場合の防御。
        if self.branch is not None:
            return
        # branch/endpoint の cached_property を無効化し、branch 作成後の再取得を
        # 強制する。endpoint はこの経路で一度も access していない (cache 未生成) ため
        # 素の `del` は AttributeError になる。access 有無に依らず安全な pop で消す。
        self.__dict__.pop("branch", None)
        self.__dict__.pop("endpoint", None)
        data = {
            "branch": {
                "name": self.context.branch_name,
            },
            "endpoints": [{"type": "read_write"}],
        }
        if base_branch:
            data["branch"]["parent_id"] = base_branch.id
        self.post("branches", data=data)

    def delete_branch(self):
        if not self.endpoint or not self.branch:
            raise Exception("Branch or endpoint not found. Something is wrong.")
        self.delete("endpoints", self.endpoint.id)
        self.delete("branches", self.branch.id)

    @property
    def branches(self) -> list[Branch]:
        """project 内の全 branch (root 判定・同居 branch の確認用)。"""
        return [Branch(**b) for b in self.get("branches").json()["branches"]]

    def delete_project(self):
        """project を丸ごと削除する。

        root branch は branch 単位で削除できないため、root を消すには project delete
        (`DELETE /projects/{project_id}`) が唯一の経路。
        """
        self.api.delete("projects/%s" % self.project.id)

    def destroy_plan(self) -> Literal["branch", "project", "blocked"]:
        """stage の branch 削除の実行計画を返す。

        - "branch": 非 root branch。従来どおり branch 単位で削除できる。
        - "project": root branch かつ project 内に他 branch が無い。branch 単位の
          削除は 422 (cannot delete the root branch) になるため project ごと削除する。
        - "blocked": root branch だが他 branch が同居 (dev project に複数 stage 等)。
          project 削除は他 stage の巻き添えになるため何も消せない。
        """
        if self.branch is None:
            raise NeonNotFound("branch '%s' not found" % self.context.branch_name)
        if self.branch.parent_id is not None:
            return "branch"
        others = [b for b in self.branches if b.id != self.branch.id]
        return "blocked" if others else "project"

    def ensure_database(self):
        if self.database is None:
            self.create_database()

    def create_database(self):
        if self.database is None:
            del self.database
        data = {
            "database": {
                "name": self.context.name,
                "owner_name": self.context.role_name,
            }
        }
        self.post("databases", data=data)

    def reset_database(self):
        if self.database:
            self.delete("databases", self.context.name)
        self.create_database()

    def set_role_password(self):
        if self.role is None:
            raise Exception("Create role first")
        if self.role.password is None:
            self.role.password = self.get(
                "roles", self.role.name + "/reveal_password"
            ).json()["password"]

    def ensure_role(self):
        if self.role is None:
            del self.role
            data = {"role": {"name": self.context.role_name}}
            self.post("roles", data=data)
            self.set_role_password()


def ensure_url_for_context(context: NeonContext) -> str:
    """`NeonContext` から branch/role/db を ensure し接続 URL を算出する。

    branch が無ければ (parent 指定があればそこから) 作成し、role と database を
    ensure したうえで、ensure 後の状態を確実に反映するため fresh instance で URL を
    算出して返す。Neon の URL は reveal_password 方式で冪等なので何度呼んでも同じ。
    """
    neon = Neon(context)
    neon.create()
    return Neon(context).database_url


def ensure_and_compute_url(
    *,
    project_name: str,
    branch_name: str,
    name: str,
    role_name: str,
    api_key: str,
    parent_branch_name: str | None = None,
    pg_version: int = 15,
) -> str:
    """pocket.toml なしで Neon の ensure + 接続 URL 算出を行う公開 API。

    外部 provisioner が backend 作成時に接続 URL を stored user secret へ焼く用途を
    想定する。SSM への保存自体は呼び出し側の責務 (正準名は
    :func:`pocket.naming.stored_user_secret_name` で導出可能)。

    Args:
        project_name: Neon project 名。
        branch_name: 対象 branch 名 (存在しなければ作成する)。
        name: database 名。
        role_name: role 名 (= 接続 URL の user)。
        api_key: Neon API key。
        parent_branch_name: branch 新規作成時の親 branch。未指定なら Neon の
            default branch から分岐する。
        pg_version: PostgreSQL メジャーバージョン。

    Returns:
        ``postgres://<role>:<password>@<host>:5432/<name>?sslmode=require`` 形式の
        接続 URL。
    """
    context = NeonContext(
        pg_version=pg_version,
        api_key=api_key,
        project_name=project_name,
        branch_name=branch_name,
        parent_branch_name=parent_branch_name,
        name=name,
        role_name=role_name,
        provisioning="command",
    )
    return ensure_url_for_context(context)
