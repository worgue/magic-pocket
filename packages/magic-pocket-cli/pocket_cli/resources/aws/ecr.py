from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING

import boto3
from pydantic import BaseModel, Field, computed_field

if TYPE_CHECKING:
    from pocket_cli.resources.aws.builders import Builder


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
        self,
        region_name: str,
        name: str,
        tag: str,
        dockerfile_path: str,
        platform: str,
        builder: Builder | None = None,
    ):
        self.client = boto3.client("ecr", region_name=region_name)
        self.name = name
        self.tag = tag
        self.dockerfile_path = dockerfile_path
        self.platform = platform
        self._builder = builder

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

    def build_and_push(self):
        if self.target is None:
            raise ValueError("target is not defined")
        if self._builder is None:
            raise ValueError("builder is not configured")
        self._builder.build_and_push(
            target=self.target,
            dockerfile_path=self.dockerfile_path,
            platform=self.platform,
        )

    def exists(self) -> bool:
        return self.info.uri is not None

    def delete(self):
        if not self.exists():
            return
        self.client.delete_repository(repositoryName=self.name, force=True)
        if hasattr(self, "info"):
            del self.info

    def sync(self):
        self.ensure_exists()
        self.build_and_push()
