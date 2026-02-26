from __future__ import annotations

import sys
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
            "namespace, project, stage(for containers), and ref(for vpc)\n"
            "e.g) {stage}-{project}-{namespace}"
        ),
    ),
]

StoreType = Literal["sm", "ssm"]


class ManagedSecretSpec(BaseModel):
    type: Literal[
        "password",
        "neon_database_url",
        "tidb_database_url",
        "rsa_pem_base64",
        "cloudfront_signing_key",
    ]
    options: dict[str, str | int] = {}
    # Used in mediator
    # PasswordOptions:
    #     length: int
    # Used in runtime
    # RsaPemBase64Options:
    #     pem_base64_environ_suffix: str = "_PEM_BASE64"
    #     pub_base64_environ_suffix: str = "_PUB_BASE64"
    # CloudFrontSigningKeyOptions:
    #     pem_base64_environ_suffix: str = "_PEM_BASE64"
    #     pub_base64_environ_suffix: str = "_PUB_BASE64"
    #     id_environ_suffix: str = "_ID"


class UserSecretSpec(BaseModel):
    name: str  # SM: ARN or secret name, SSM: parameter name/path
    store: StoreType | None = None  # Noneの場合Secrets.storeを継承


class Secrets(BaseModel):
    store: StoreType = "sm"
    pocket_key_format: Annotated[
        FormatStr,
        Field(
            description=(
                "Format string for pocket key. e.g) {stage}-{project}-{namespace}\n"
                "You can use variables: namespace, project, stage\n"
                "Although default value contains stage and project, "
                "it is not required. Because the secret value is stored "
                "under the stage and project key in json.\n"
                "If you remove stage or project from the key, be careful "
                "not to generate secret keys simaltaneously in different situations.\n"
                "It might cause a race condition."
            )
        ),
    ] = "{stage}-{project}-{namespace}"
    managed: Annotated[
        dict[EnvStr, ManagedSecretSpec],
        Field(
            description=(
                "These secrets are managed by magic-pocket, "
                "magic-pocket create secrets when creating lambda container."
            )
        ),
    ] = {}
    user: Annotated[
        dict[EnvStr, UserSecretSpec],
        Field(
            description=(
                "These secrets get GetSecretValue/GetParameter permissions "
                "automatically based on their store type.\n"
                "You still need to create them by yourself."
            )
        ),
    ] = {}
    extra_resources: Annotated[
        list[str],
        Field(
            description=(
                "List secret ARNs to allow GetSecretValue/GetParameter, "
                "if you want to access them from your own lambda functions.\n"
                "Supports both SM and SSM ARNs."
            )
        ),
    ] = []
    require_list_secrets: bool = False


class AwsContainer(BaseModel):
    vpc: Vpc | None = None
    secrets: Secrets | None = None
    handlers: dict[str, LambdaHandler] = {}
    dockerfile_path: str
    envs: dict[str, str] = {}
    platform: str = "linux/amd64"
    django: Django | None = None
    permissions_boundary: str | None = None

    @model_validator(mode="after")
    def check_handlers(self):
        check_command = "pocket.django.lambda_handlers.management_command_handler"
        commend_list = [h for h in self.handlers.values() if h.command == check_command]
        if 1 < len(commend_list):
            raise ValueError("Only one management command handler is allowed.")
        return self


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
    project_name: str
    pg_version: int = 15
    api_key: str | None = Field(alias="neon_api_key", default=None)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


class TiDb(BaseSettings):
    public_key: str | None = Field(alias="tidb_public_key", default=None)
    private_key: str | None = Field(alias="tidb_private_key", default=None)
    project: str | None = None
    cluster: str | None = None
    region: str = "ap-northeast-1"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


class S3(BaseSettings):
    bucket_name_format: FormatStr = "{stage}-{project}-{namespace}"


class RedirectFrom(BaseSettings):
    domain: str
    hosted_zone_id_override: str | None = None


class Route(BaseSettings):
    type: Literal["s3", "api"] = "s3"
    handler: str | None = None
    path_pattern: str = ""
    is_default: bool = False
    is_spa: bool = False
    is_versioned: bool = False
    spa_fallback_html: str = "index.html"
    versioned_max_age: int = 60 * 60 * 24 * 365
    ref: str = ""
    signed: bool = False
    build: str | None = None
    build_dir: str | None = None

    @model_validator(mode="after")
    def check_api_route(self):
        if self.type == "api":
            if not self.handler:
                raise ValueError("handler is required when type = 'api'")
            if self.is_spa or self.is_versioned or self.signed or self.is_default:
                raise ValueError(
                    "type = 'api' cannot use "
                    "is_spa, is_versioned, signed, or is_default"
                )
            if self.build or self.build_dir:
                raise ValueError("type = 'api' cannot use build or build_dir")
        if self.handler and self.type != "api":
            raise ValueError("handler requires type = 'api'")
        return self

    @model_validator(mode="after")
    def check_build(self):
        if self.build and not self.build_dir:
            raise ValueError("build_dir is required when build is set")
        return self

    @model_validator(mode="after")
    def check_flags(self):
        if self.is_spa and self.is_versioned:
            raise ValueError("is_spa and is_versioned cannot be True at the same time")
        return self

    @model_validator(mode="after")
    def check_is_default(self):
        if self.type == "api":
            return self
        if self.is_default and self.path_pattern:
            raise ValueError("is_default route must have empty path_pattern")
        if not self.is_default and not self.path_pattern:
            raise ValueError(
                "route with empty path_pattern must have is_default = true"
            )
        return self

    @model_validator(mode="after")
    def check_path_pattern(self):
        if self.path_pattern:
            if self.path_pattern[0] != "/":
                raise ValueError("non default path_pattern must starts with /")
            if self.path_pattern[-1] == "/":
                raise ValueError("path_pattern must not ends with /")
        return self

    @model_validator(mode="after")
    def check_ref(self):
        if self.ref:
            if self.path_pattern[-2:] != "/*":
                raise ValueError("When ref is set, path_pattern must starts with /*")
        return self


class CloudFront(BaseSettings):
    domain: str | None = None
    origin_prefix: str = "/spa"
    hosted_zone_id_override: str | None = None
    redirect_from: list[RedirectFrom] = []
    routes: list[Route] = []
    signing_key: str | None = None

    @model_validator(mode="after")
    def check_origin_prefix(self):
        if self.origin_prefix:
            if self.origin_prefix[0] != "/":
                raise ValueError("origin_prefix must starts with /")
            if self.origin_prefix[-1] == "/":
                raise ValueError("origin_prefix must not ends with /")
        return self

    @model_validator(mode="after")
    def check_domain_redirect_from(self):
        if self.domain is None and self.redirect_from:
            raise ValueError("redirect_from requires domain to be set")
        return self

    @model_validator(mode="after")
    def check_routes(self):
        if len(self.routes) == 0:
            raise ValueError("routes must have at least one route")
        defaults = [r for r in self.routes if r.is_default]
        if len(defaults) != 1:
            raise ValueError("routes must have exactly one is_default = true route")
        return self


class Settings(BaseSettings):
    general: GeneralSettings
    stage: TagStr
    awscontainer: AwsContainer | None = None
    neon: Neon | None = None
    tidb: TiDb | None = None
    s3: S3 | None = None
    cloudfront: dict[str, CloudFront] = {}

    @property
    def project_name(self):
        return self.general.project_name

    @property
    def region(self):
        return self.general.region

    @property
    def namespace(self):
        return self.general.namespace

    @property
    def prefix_template(self):
        return self.general.prefix_template

    @computed_field
    @property
    def slug(self) -> str:
        """Identify the environment. e.g) dev-myprj"""
        return "%s-%s" % (self.stage, self.general.project_name)

    @model_validator(mode="after")
    def check_cloudfront_requires_s3(self):
        if self.cloudfront and not self.s3:
            raise ValueError("s3 is required when cloudfront is configured")
        for name, cf in self.cloudfront.items():
            for route in cf.routes:
                if route.signed and not cf.signing_key:
                    raise ValueError(
                        f"cloudfront.{name}: signing_key is required "
                        f"when route has signed=true"
                    )
                if route.type == "api":
                    assert route.handler
                    if not self.awscontainer:
                        raise ValueError(
                            f"cloudfront.{name}: awscontainer is required "
                            f"when route has type='api'"
                        )
                    if route.handler not in self.awscontainer.handlers:
                        raise ValueError(
                            f"cloudfront.{name}: handler '{route.handler}' "
                            f"not found in awscontainer.handlers"
                        )
                    handler = self.awscontainer.handlers[route.handler]
                    if not handler.apigateway:
                        raise ValueError(
                            f"cloudfront.{name}: handler '{route.handler}' "
                            f"must have apigateway configured for api route"
                        )
        return self

    @classmethod
    def from_toml(cls, *, stage: str):
        data = tomllib.loads(get_toml_path().read_text())
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
        valid_keys = ["general", "awscontainer", "neon", "tidb", "s3", "cloudfront"]
        valid_keys += data["general"]["stages"]
        for key in data:
            if key not in valid_keys:
                error = f"invalid key {key} in pocket.toml\n"
                error += "If it's a stage name, add it to stages."
                raise ValueError(error)
        # cloudfront はサブテーブル形式 [cloudfront.xxx]
        # TOML パース結果が dict of dict であることを確認
        if "cloudfront" in data:
            if isinstance(data["cloudfront"], dict):
                for v in data["cloudfront"].values():
                    if not isinstance(v, dict):
                        break

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
