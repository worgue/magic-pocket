from __future__ import annotations

import json
import logging
import os
import time
from functools import cached_property
from typing import TYPE_CHECKING, Literal

import requests
from pydantic import BaseModel

from .base import ResourceStatus

if TYPE_CHECKING:
    from pocket.context import NeonContext

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(level=os.getenv("POCKET_LOGGER_LEVEL", "WARNING").upper())

ResourceType = Literal["projects", "branches", "databases", "endpoints", "roles"]


class NeonResourceIsNotReady(Exception):
    pass


class Project(BaseModel):
    id: str
    name: str


class Branch(BaseModel):
    id: str
    name: str


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

    def get(self, path):
        logger.info("GET %s" % self.endpoint + path)
        res = requests.get(self.endpoint + path, headers=self.header)
        logger.debug(res.status_code)
        logger.debug(json.dumps(res.json(), indent=2))
        if 200 <= res.status_code < 300:
            return res
        if res.status_code == 401:
            print("Used API key: %s" % (self.key[:5] + "..." + self.key[-5:]))
            print("API key length: %s" % len(self.key))
        raise Exception("%s: %s" % (res.status_code, res.json()["message"]))

    def post(self, path, data=None):
        logger.warning("POST %s" % self.endpoint + path)
        logger.debug(json.dumps(data, indent=2))
        res = requests.post(self.endpoint + path, headers=self.header, json=data)
        logger.debug(res.status_code)
        logger.debug(json.dumps(res.json(), indent=2))
        if 200 <= res.status_code < 300:
            time.sleep(2)
            return res
        if res.status_code == 401:
            print("Used API key: %s" % (self.key[:5] + "..." + self.key[-5:]))
            print("API key length: %s" % len(self.key))
        raise Exception("%s: %s" % (res.status_code, res.json()["message"]))

    def delete(self, path, data=None):
        logger.warning("DELETE %s" % self.endpoint + path)
        logger.debug(json.dumps(data, indent=2))
        res = requests.delete(self.endpoint + path, headers=self.header, json=data)
        logger.debug(res.status_code)
        logger.debug(json.dumps(res.json(), indent=2))
        if 200 <= res.status_code < 300:
            time.sleep(2)
            return res
        raise Exception("%s: %s" % (res.status_code, res.json()["message"]))

    def projects_url(self):
        return self.endpoint + "projects"


class Neon:
    context: NeonContext

    def __init__(self, context: NeonContext) -> None:
        self.context = context

    def get_resource_path(self, resource_type: ResourceType) -> str:
        requirements = {
            "projects": [],
            "branches": ["project"],
            "databases": ["project", "branch"],
            "endpoints": ["project"],
            "roles": ["project", "branch"],
        }
        path_templates = {
            "projects": "projects",
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
        if self.branch:
            res = self.get("roles", self.context.role_name)
            if res.status_code == 404:
                return None
            return Role(**res.json()["role"])

    @cached_property
    def project(self) -> Project | None:
        for project in self.get("projects").json().get("projects", []):
            if project["name"] == self.context.project_name:
                return Project(**project)

    @cached_property
    def branch(self) -> Branch | None:
        if self.project:
            for branch in self.get("branches").json()["branches"]:
                if branch["name"] == self.context.branch_name:
                    return Branch(**branch)

    @cached_property
    def database(self) -> Database | None:
        if self.branch:
            for database in self.get("databases").json()["databases"]:
                if database["name"] == self.context.name:
                    return Database(**database)

    @cached_property
    def endpoint(self) -> Endpoint | None:
        if self.branch:
            for endpoint in self.get("endpoints").json()["endpoints"]:
                if endpoint["branch_id"] == self.branch.id:
                    return Endpoint(**endpoint)

    @property
    def database_url(self):
        if self.role is None or self.endpoint is None:
            raise NeonResourceIsNotReady("Create role and endpoint first")
        if self.role.password is None:
            self.set_role_password()
        return "postgres://%s:%s@%s:5432/%s" % (
            self.context.role_name,
            self.role.password,
            self.endpoint.host,
            self.context.name,
        )

    @property
    def status(self) -> ResourceStatus:
        if self.working:
            return "COMPLETED"
        return "NOEXIST"

    @property
    def working(self):
        check = [self.project, self.branch, self.database, self.endpoint, self.role]
        logger.info(str(check))
        return all(check)

    @property
    def description(self):
        return "Create Neon project, branch, database, role, and endpoint"

    def create_new(self):
        self.create()
        self.reset_database()

    def deploy_init(self):
        pass

    def create(self):
        self.ensure_project()
        self.create_branch()
        self.ensure_role()
        self.ensure_database()

    def ensure_project(self):
        if self.project is None:
            del self.project
            data = {
                "project": {
                    "pg_version": self.context.pg_version,
                    "name": self.context.project_name,
                    "region_id": self.context.region_id,
                }
            }
            self.post("projects", data=data)
        return self.project

    def create_branch(self, base_branch: Branch | None = None):
        if self.branch is None:
            del self.branch
            del self.endpoint
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

    def create_role(self):
        if self.role is None:
            del self.role
        data = {"role": {"name": self.context.role_name}}
        self.post("roles", data=data)

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
