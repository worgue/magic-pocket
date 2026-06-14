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

    def build_and_push(self, tag: str | None = None):
        if self.uri is None:
            raise ValueError("target is not defined")
        # tag 省略時は self.tag (= stage) を使う。build once 用に commit hash 等を
        # 焼きたい場合は tag を明示する。
        target = self.uri + ":" + (tag or self.tag)
        if self._builder is None:
            raise ValueError("builder is not configured")
        self._builder.build_and_push(
            target=target,
            dockerfile_path=self.dockerfile_path,
            platform=self.platform,
        )

    def retag(self, source_tag: str, dest_tag: str):
        """source_tag の image に dest_tag を付与する (タグの付け替え。build しない)。

        build once の昇格用: `:<commit hash>` の image へ `:<stage>` タグを移す。
        source_tag の image が存在しない場合は ValueError。
        """
        try:
            images = self.client.batch_get_image(
                repositoryName=self.name,
                imageIds=[{"imageTag": source_tag}],
            )["images"]
        except self.client.exceptions.RepositoryNotFoundException as e:
            raise ValueError(
                "ECR repository '%s' が存在しません。"
                "先に `pocket django build` を実行してください。" % self.name
            ) from e
        if not images:
            raise ValueError(
                "image :%s が ECR repository '%s' に存在しません。"
                "先に `pocket django build` を実行してください。"
                % (source_tag, self.name)
            )
        try:
            self.client.put_image(
                repositoryName=self.name,
                imageManifest=images[0]["imageManifest"],
                imageTag=dest_tag,
            )
        except self.client.exceptions.ImageAlreadyExistsException:
            pass  # dest_tag が既に同じ digest を指している (再昇格の冪等性)

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
