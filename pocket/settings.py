from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Any, Literal

import mergedeep
from pydantic import BaseModel, Field, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .utils import get_project_name

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

# Restrict string to a valid Docker tag
TagStr = Annotated[str, Field(pattern="^[a-z0-9][a-z0-9._-]*$", max_length=128)]

# Formatted string
FormatStr = Annotated[
    str,
    Field(
        pattern="^[a-z0-9{][{}a-z0-9._-]*$",
        max_length=128,
        description=(
            "Formatted string."
            "You can use variables: prefix, project, stage(for containers), and ref(for vpc)"
            "e.g) {prefix}{stage}-{project}"
        ),
    ),
]


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
                "lambda runs in private subnet, internet_gateway without nat_gateway is not supported yet."
                "It will be supported in the future with fargate."
            )
        return self


class DjangoStorage(BaseSettings):
    store: Literal["s3"]
    location: str
    static: bool = False
    manifest: bool = False

    @model_validator(mode="after")
    def check_manifest(self):
        if self.manifest and not self.static:
            raise ValueError("manifest can only be used with static")
        return self


class DjangoCache(BaseSettings):
    store: Literal["efs"]
    subdir: str = "{stage}"


class Django(BaseSettings):
    storages: dict[str, DjangoStorage] = {}
    caches: dict[str, DjangoCache] = {}
    settings: dict[str, Any] = {}


class AwsContainer(BaseModel):
    vpc: Vpc | None = None
    secretsmanager: SecretsManager | None = None
    handlers: dict[str, LambdaHandler] = {}
    dockerfile_path: str
    envs: dict[str, str] = {}
    use_public_internet_access: bool = True
    platform: str = "linux/amd64"
    django: Django | None = None


class SecretsManager(BaseSettings):
    secrets: dict[str, str] = {}


class LambdaHandler(BaseModel):
    command: str
    timeout: int = 30
    memory_size: int = 512
    apigateway: ApiGateway | None = None
    sqs: Sqs | None = None


class ApiGateway(BaseModel):
    domain: str | None = None
    hosted_zone_id_override: str | None = None

    @computed_field
    @property
    def disable_execute_api_endpoint(self) -> bool:
        return bool(self.domain)


class Sqs(BaseModel):
    batch_size: int = 10
    message_retention_period: int = 345600
    # minimum 2
    # https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-properties-lambda-eventsourcemapping-scalingconfig.html#aws-properties-lambda-eventsourcemapping-scalingconfig-properties
    maximum_concurrency: int = 2
    # set the maxReceiveCount on the source queue's redrive policy to at least 5
    # https://docs.aws.amazon.com/lambda/latest/dg/with-sqs.html#events-sqs-queueconfig
    dead_letter_max_receive_count: int = 5
    dead_letter_message_retention_period: int = 1209600
    report_batch_item_failures: bool = True


class Neon(BaseSettings):
    pg_version: int = 15
    api_key: str = Field(alias="neon_api_key")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


class S3(BaseSettings):
    public_dirs: list[str] = []
    bucket_name_format: FormatStr = "{prefix}{stage}-{project}"


class Settings(BaseSettings):
    region: str
    project_name: str = Field(default_factory=get_project_name)
    stage: TagStr
    awscontainer: AwsContainer | None = None
    neon: Neon | None = None
    s3: S3 | None = None

    model_config = SettingsConfigDict(env_prefix="pocket_")

    @computed_field
    @property
    def slug(self) -> str:
        """Identify the environment. e.g) dev-myprj"""
        return "%s-%s" % (self.stage, self.project_name)

    @classmethod
    def from_toml(cls, *, stage: str, path: str | Path | None = None, filters=None):
        path = path or "pocket.toml"
        data = tomllib.loads(Path(path).read_text())
        cls.check_keys(data)
        cls.check_stage(stage, data)
        cls.merge_stage_data(stage, data)
        cls.remove_stages_data(stage, data)
        if filters:
            new_data = {}
            for f in filters:
                data_target = data
                new_data_target = new_data
                for key in f.split(".")[:-1]:
                    data_target = data_target[key]
                    new_data_target = new_data_target.setdefault(key, {})
                key = f.split(".")[-1]
                new_data_target[key] = data_target[key]
            data = new_data
        data["stage"] = stage
        cls.check_vpc(data)
        cls.pop_vpc(data)
        return cls.model_validate(data)

    @classmethod
    def check_vpc(cls, data: dict):
        if vpc_ref := data.get("awscontainer", {}).get("vpc_ref"):
            if "vpcs" not in data:
                raise ValueError("vpcs is required when vpc_ref is used")
            if vpc_ref not in data["vpcs"]:
                raise ValueError(f"vpc {vpc_ref} not found in vpcs")

    @classmethod
    def pop_vpc(cls, data: dict):
        if vpc_ref := data.get("awscontainer", {}).get("vpc_ref"):
            data["awscontainer"]["vpc"] = data["vpcs"][vpc_ref]
            data["awscontainer"]["vpc"]["ref"] = vpc_ref
            data["awscontainer"].pop("vpc_ref", None)
        data.pop("vpcs", None)

    @classmethod
    def check_keys(cls, data: dict):
        valid_keys = (
            ["project_name", "region", "stages", "vpcs"]
            + ["awscontainer", "neon", "s3"]
            + ["django"]
            + data["stages"]
        )
        for key in data:
            if key not in valid_keys:
                error = f"invalid key {key} in pocket.toml\n"
                error += "If it's a stage name, add it to stages."
                raise ValueError(error)

    @classmethod
    def check_stage(cls, stage: str, data: dict):
        if stage not in data["stages"]:
            raise ValueError(f"stage {stage} not found in {data['stages']}")

    @classmethod
    def merge_stage_data(cls, stage: str, data: dict):
        mergedeep.merge(data, data.get(stage, {}))

    @classmethod
    def remove_stages_data(cls, stage: str, data: dict):
        for s in data["stages"]:
            data.pop(s, None)
        del data["stages"]
