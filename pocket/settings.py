from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import mergedeep
from pydantic import BaseModel, Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .utils import get_project_name

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

# Restrict string to a valid Docker tag
TagStr = Annotated[str, Field(pattern="^[a-z0-9]+[a-z0-9._-]*$", max_length=128)]


class AwsContainer(BaseModel):
    handlers: dict[str, AwslambdaHandler] = {}
    dockerfile_path: str
    envs: dict[str, str] = {}
    use_public_internet_access: bool = True
    platform: str = "linux/amd64"


class AwslambdaHandler(BaseModel):
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
    api_key: str | None = Field(alias="neon_api_key", default=None)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


class S3(BaseSettings):
    public_dirs: list[str] = []


class SecretsManager(BaseSettings):
    secrets: dict[str, str] = {}


services = ["awscontainer", "secretsmanager", "neon", "s3"]


class Settings(BaseSettings):
    region: str
    object_prefix: str = "pocket-"
    project_name: str = Field(default_factory=get_project_name)
    stage: TagStr
    awscontainer: AwsContainer | None = None
    neon: Neon | None = None
    s3: S3 | None = None
    secretsmanager: SecretsManager | None = None

    model_config = SettingsConfigDict(env_prefix="pocket_")

    @computed_field
    @property
    def slug(self) -> str:
        """Identify the environment. e.g) dev-myprj"""
        return "%s-%s" % (self.stage, self.project_name)

    @property
    def services(self):
        return services

    @classmethod
    def from_toml(cls, *, stage: str, path: str | Path = Path("pocket.toml")):
        path = cls.ensure_path(path)
        data = tomllib.loads(path.read_text())
        cls.check_keys(data)
        cls.check_stage(stage, data)
        cls.merge_stage_data(stage, data)
        cls.remove_stages_data(stage, data)
        data["stage"] = stage
        return cls.model_validate(data)

    @classmethod
    def ensure_path(cls, path: str | Path) -> Path:
        if isinstance(path, str):
            path = Path(path)
        if not path.exists():
            raise FileNotFoundError(path)
        return path

    @classmethod
    def check_keys(cls, data: dict):
        valid_keys = ["project_name", "region", "stages"] + services + data["stages"]
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
