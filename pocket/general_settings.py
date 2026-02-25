from __future__ import annotations

import sys
from typing import Annotated

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings

from .django.settings import Django
from .utils import get_project_name, get_toml_path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


class GeneralSettings(BaseSettings):
    namespace: str = "pocket"
    prefix_template: str = "{stage}-{project}-{namespace}-"
    region: str
    project_name: str = Field(default_factory=get_project_name)
    stages: list[str]
    vpcs: list[Vpc] = []
    s3_fallback_bucket_name: str | None = None
    django_fallback: Django = Django()

    @classmethod
    def from_toml(cls):
        data = tomllib.loads(get_toml_path().read_text())
        return cls.model_validate(data.get("general", {}))


class Efs(BaseModel):
    local_mount_path: str = Field(pattern="^/mnt/.*", default="/mnt/efs")
    access_point_path: str = Field(pattern="^/.+", default="/lambda")


class Vpc(BaseSettings):
    ref: str
    zone_suffixes: list[Annotated[str, Field(max_length=1)]] = ["a"]
    nat_gateway: bool = True
    internet_gateway: bool = True
    efs: Efs | None = None

    @model_validator(mode="after")
    def check_nat_gateway(self):
        if self.nat_gateway and not self.internet_gateway:
            raise ValueError("nat_gateway without internet_gateway is not supported.")
        if self.internet_gateway and not self.nat_gateway:
            raise ValueError(
                "lambda runs in private subnet, internet_gateway without nat_gateway is"
                " not supported yet.\nWe should support it in the future if we want to "
                "support fargate."
            )
        return self
