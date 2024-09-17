from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING

import boto3
from pydantic import BaseModel, Field, computed_field
from python_on_whales import docker

if TYPE_CHECKING:
    pass


class RepositoryDetail(BaseModel):
    uri: str | None = Field(alias="repositoryUri", default=None)
    arn: str | None = Field(alias="repositoryArn", default=None)


class ImageDetail(BaseModel):
    image_digest: str | None = Field(alias="imageDigest", default=None)

    @computed_field
    def hash(self) -> str | None:
        if self.image_digest:
            alg, hash = self.image_digest.split(":")
            if alg == "sha256":
                return hash
            raise ValueError("unsupported algorithm")


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
    def info(self) -> RepositoryDetail:
        for repository in self.client.describe_repositories()["repositories"]:
            if repository["repositoryName"] == self.name:
                return RepositoryDetail(**repository)
        return RepositoryDetail()

    @property
    def uri(self):
        return self.info.uri

    @property
    def arn(self):
        return self.info.arn

    @property
    def target(self):
        if self.uri:
            return self.uri + ":" + self.tag

    @property
    def image_detail(self):
        data = self.client.describe_images(repositoryName=self.name)
        for detail in data["imageDetails"]:
            if image_tags := detail.get("imageTags"):
                if self.tag in image_tags:
                    return ImageDetail(**detail)
        return ImageDetail()

    def create(self):
        print("Creating repository ...")
        print("  %s" % self.name)
        self.client.create_repository(repositoryName=self.name)
        if hasattr(self, "info"):
            del self.info
        print("  %s" % self.uri)

    def ensure_exists(self):
        if not self.info.uri:
            self.create()

    def build(self):
        if self.target is None:
            raise ValueError("target is not defined")
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
        if self.target is None:
            raise ValueError("target is not defined")
        self.ensure_exists()
        print("Logging in to ecr...")
        docker.login_ecr(region_name=self.client.meta.config.region_name)
        print("Pushing docker image...")
        docker.push(self.target)

    def sync(self):
        self.ensure_exists()
        self.build()
        self.push()
