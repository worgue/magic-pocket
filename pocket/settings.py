from __future__ import annotations

import sys
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated, Literal

import mergedeep
from pydantic import BaseModel, Field, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .django.settings import Django
from .general_settings import GeneralSettings, Vpc
from .utils import get_toml_path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

context_settings: ContextVar[Settings] = ContextVar("context_settings")

# Restrict string to a valid environment variable name
EnvStr = Annotated[str, Field(pattern="^[a-zA-Z0-9_]+$")]

# Restrict string to a valid Docker tag
TagStr = Annotated[str, Field(pattern="^[a-z0-9][a-z0-9._-]*$", max_length=128)]

# Formatted string
FormatStr = Annotated[
    str,
    Field(
        pattern="^[a-z0-9{][{}a-z0-9._-]*$",
        max_length=128,
        description=(
            "Formatted string. You can use variables: "
            "prefix, project, stage(for containers), and ref(for vpc)\n"
            "e.g) {prefix}{stage}-{project}"
        ),
    ),
]


class AwsContainer(BaseModel):
    vpc: Vpc | None = None
    secretsmanager: SecretsManager | None = None
    handlers: dict[str, LambdaHandler] = {}
    dockerfile_path: str
    envs: dict[str, str] = {}
    platform: str = "linux/amd64"
    django: Django | None = None

    @model_validator(mode="after")
    def check_handlers(self):
        check_command = "pocket.django.lambda_handlers.management_command_handler"
        commend_list = [h for h in self.handlers.values() if h.command == check_command]
        if 1 < len(commend_list):
            raise ValueError("Only one management command handler is allowed.")
        return self


class PocketSecretSpec(BaseModel):
    type: Literal["password", "neon_database_url", "rsa_pem_base64"]
    options: dict[str, str | int] = {}
    # Used in mediator
    # PasswordOptions:
    #     length: int
    # Used in runtime
    # RsaPemBase64Options:
    #     pem_base64_environ_suffix: str = "_PEM_BASE64"
    #     pub_base64_environ_suffix: str = "_PUB_BASE64"


class SecretsManager(BaseSettings):
    pocket_key_format: Annotated[
        FormatStr,
        Field(
            description=(
                "Format string for pocket key. e.g) {prefix}{stage}-{project}\n"
                "You can use variables: prefix, project, stage\n"
                "Although default value contains stage and project, "
                "it is not required. Because the secret value is stored "
                "under the stage and project key in json.\n"
                "If you remove stage or project from the key, be careful "
                "not to generate secret keys simaltaneously in different situations.\n"
                "It might cause a race condition."
            )
        ),
    ] = "{prefix}{stage}-{project}"
    pocket_secrets: Annotated[
        dict[EnvStr, PocketSecretSpec],
        Field(
            description=(
                "These secrets are managed by magic-pocket, "
                "magic-pocket create secrets when creating lambda container."
            )
        ),
    ] = {}
    secrets: Annotated[
        dict[EnvStr, str],
        Field(
            description=(
                "These secres got GetSecretValue permissions automatically, "
                "so does not need to be explicitly defined in resources.\n"
                "But, you still need to create it by yourself."
            )
        ),
    ] = {}
    extra_resources: Annotated[
        list[str],
        Field(
            description=(
                "List secret names or regexp to allow GetSecretValue, "
                "if you want to access them from your own lambda functions."
            )
        ),
    ] = []
    require_list_secrets: bool = False


class LambdaHandler(BaseModel):
    command: str
    timeout: int = 30
    memory_size: int = 512
    reserved_concurrency: int | None = None
    apigateway: ApiGateway | None = None
    sqs: Sqs | None = None


class ApiGateway(BaseSettings):
    domain: str | None = None
    create_records: bool = True
    hosted_zone_id_override: str | None = None


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
    bucket_name_format: FormatStr = "{prefix}{stage}-{project}"


class RedirectFrom(BaseSettings):
    domain: str
    hosted_zone_id_override: str | None = None


class Spa(BaseSettings):
    domain: str
    bucket_name_format: FormatStr = "{prefix}{stage}-{project}-spa"
    origin_path_format: str = "/{stage}"
    fallback_html: str = "index.html"
    hosted_zone_id_override: str | None = None
    redirect_from: list[RedirectFrom] = []

    @model_validator(mode="after")
    def check_origin_path(self):
        if self.origin_path_format:
            if self.origin_path_format[0] != "/":
                raise ValueError("origin_path_format must starts with /")
            if self.origin_path_format[-1] == "/":
                raise ValueError("origin_path_format must not ends with /")
        return self

    @model_validator(mode="after")
    def check_path(self):
        path = self.bucket_name_format + self.origin_path_format
        if "{stage}" not in path:
            raise ValueError(
                "{stage} must exists in origin_path_format or bucket_name_format"
            )
        if "{project}" not in path:
            raise ValueError(
                "{project} must exists in origin_path_format or bucket_name_format"
            )
        return self


class Settings(BaseSettings):
    general: GeneralSettings
    stage: TagStr
    awscontainer: AwsContainer | None = None
    neon: Neon | None = None
    s3: S3 | None = None
    spa: Spa | None = None

    @property
    def project_name(self):
        return self.general.project_name

    @property
    def region(self):
        return self.general.region

    @property
    def object_prefix(self):
        return self.general.object_prefix

    @computed_field
    @property
    def slug(self) -> str:
        """Identify the environment. e.g) dev-myprj"""
        return "%s-%s" % (self.stage, self.general.project_name)

    @classmethod
    def from_toml(cls, *, stage: str, path: str | Path | None = None):
        path = path or get_toml_path()
        data = tomllib.loads(Path(path).read_text())
        cls.check_keys(data)
        cls.check_stage(stage, data)
        cls.merge_stage_data(stage, data)
        cls.remove_stages_data(stage, data)
        data["stage"] = stage
        cls.process_vpc_ref(data)
        return cls.model_validate(data)

    @classmethod
    def process_vpc_ref(cls, data: dict):
        if "awscontainer" not in data:
            return
        if "vpc_ref" not in data["awscontainer"]:
            return
        vpc_ref = data["awscontainer"].pop("vpc_ref")
        for vpc_data in data["general"]["vpcs"]:
            if vpc_data["ref"] == vpc_ref:
                data["awscontainer"]["vpc"] = vpc_data
                break
        else:
            raise ValueError(f"vpc {vpc_ref} not found in general.vpcs")

    @classmethod
    def check_keys(cls, data: dict):
        valid_keys = ["general", "awscontainer", "neon", "s3"]
        valid_keys += data["general"]["stages"]
        for key in data:
            if key == "spa":
                raise ValueError(
                    "Currently spa is not supported for top level. Use it under stage."
                )
            if key not in valid_keys:
                error = f"invalid key {key} in pocket.toml\n"
                error += "If it's a stage name, add it to stages."
                raise ValueError(error)

    @classmethod
    def check_stage(cls, stage: str, data: dict):
        if stage not in data["general"]["stages"]:
            raise ValueError(f"stage {stage} not found in {data['general']['stages']}")

    @classmethod
    def merge_stage_data(cls, stage: str, data: dict):
        mergedeep.merge(data, data.get(stage, {}))

    @classmethod
    def remove_stages_data(cls, stage: str, data: dict):
        for s in data["general"]["stages"]:
            data.pop(s, None)
