from __future__ import annotations

import base64
from functools import cached_property
from typing import TYPE_CHECKING

import boto3
from pydantic import BaseModel
from python_on_whales import docker

if TYPE_CHECKING:
    pass


class RepositoryDetail(BaseModel):
    repositoryUri: str


class Ecr:
    def __init__(
        self, region_name: str, name: str, tag: str, dockerfile_path: str, platform: str
    ):
        self.client = boto3.client("ecr", region_name=region_name)
        self.name = name
        self.tag = tag
        self.dockerfile_path = dockerfile_path
        self.platform = platform

    @cached_property
    def info(self) -> RepositoryDetail | None:
        for repository in self.client.describe_repositories()["repositories"]:
            if repository["repositoryName"] == self.name:
                return RepositoryDetail(**repository)

    @property
    def repository_uri(self):
        if not self.info:
            raise Exception("Repository not found.")
        return self.info.repositoryUri

    @property
    def target(self):
        return self.repository_uri + ":" + self.tag

    def create(self):
        print("Creating repository ...")
        print("  %s" % self.name)
        self.client.create_repository(repositoryName=self.name)
        if hasattr(self, "info"):
            del self.info
        print("  %s" % self.repository_uri)

    def ensure_exists(self):
        if not self.info:
            self.create()

    def build(self):
        dockerfile_path = self.dockerfile_path
        platform = self.platform
        print("Building docker image...")
        print("  dockerpath: %s" % dockerfile_path)
        print("  tags: %s" % self.target)
        print("  platforms: %s" % platform)
        docker.build(
            ".",
            file=str(dockerfile_path),
            tags=self.target,
            platforms=[platform],
        )

    def push(self):
        self.ensure_exists()
        token = self.client.get_authorization_token()
        username, password = (
            base64.b64decode(token["authorizationData"][0]["authorizationToken"])
            .decode()
            .split(":")
        )
        print("Logging in to ecr...")
        docker.login(self.repository_uri, username, password)
        print("Pushing docker image...")
        docker.push(self.target)

    def sync(self):
        self.ensure_exists()
        self.build()
        self.push()
