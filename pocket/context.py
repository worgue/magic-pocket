from __future__ import annotations

import sys
from contextvars import ContextVar
from functools import cached_property
from pathlib import Path

from pydantic import computed_field, model_validator
from pydantic_settings import SettingsConfigDict

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib
from . import settings
from .resources.aws.secretsmanager import SecretsManager
from .resources.awscontainer import AwsContainer
from .resources.neon import Neon
from .resources.s3 import S3
from .resources.vpc import Vpc
from .utils import get_hosted_zone_id_from_domain, get_project_name

context_settings: ContextVar[settings.Settings] = ContextVar("context_settings")


class VpcContext(settings.Vpc):
    ref: str
    name: str
    region: str

    @computed_field
    @property
    def private_route_table(self) -> bool:
        # If we support vpcendpoints, they require private_route_table
        return self.nat_gateway

    @computed_field
    @property
    def zones(self) -> list[str]:
        return [f"{self.region}{suffix}" for suffix in self.zone_suffixes]

    @model_validator(mode="before")
    @classmethod
    def context(cls, data: dict) -> dict:
        settings = context_settings.get()
        data["region"] = settings.region
        data["name"] = settings.project_name + "-" + data["ref"]
        return data

    @classmethod
    def from_toml(cls, *, ref: str, path: str | Path = Path("pocket.toml")):
        data = tomllib.loads(Path(path).read_text())

        class MockContext:
            region = data["region"]
            project_name = data.get("project_name") or get_project_name()

        if "vpcs" not in data:
            raise ValueError("vpcs is required when vpc_ref is used")
        if ref not in data["vpcs"]:
            raise ValueError(f"vpc {ref} not found in vpcs")
        vpc_data = data.get("vpcs", {}).get(ref)
        vpc_data["ref"] = ref
        token = context_settings.set(MockContext)  # pyright: ignore
        try:
            print(f"{vpc_data=}")
            return cls.model_validate(vpc_data)
        finally:
            context_settings.reset(token)

    @cached_property
    def resource(self):
        return Vpc(self)


class ApiGatewayContext(settings.ApiGateway):
    @computed_field
    @cached_property
    def hosted_zone_id(self) -> str | None:
        if self.hosted_zone_id_override:
            return self.hosted_zone_id_override
        if not self.domain:
            return None
        return get_hosted_zone_id_from_domain(self.domain)


class SqsContext(settings.Sqs):
    name: str
    visibility_timeout: int


class LambdaHandlerContext(settings.LambdaHandler):
    region: str
    apigateway: ApiGatewayContext | None
    sqs: SqsContext | None
    key: str
    function_name: str
    log_group_name: str

    @model_validator(mode="before")
    @classmethod
    def context(cls, data: dict) -> dict:
        settings = context_settings.get()
        data["region"] = settings.region
        data["function_name"] = f"{settings.slug}-{data['key']}"
        data["log_group_name"] = f"/aws/lambda/{data['function_name']}"
        if data["sqs"]:
            data["sqs"]["name"] = f"{settings.slug}-{data['key']}"
            data["sqs"]["visibility_timeout"] = data["timeout"] * 6
        return data


class SecretsManagerContext(settings.SecretsManager):
    region: str

    @cached_property
    def resource(self):
        return SecretsManager(self)

    @model_validator(mode="before")
    @classmethod
    def context(cls, data: dict) -> dict:
        settings = context_settings.get()
        data["region"] = settings.region
        return data


class AwsContainerContext(settings.AwsContainer):
    vpc: VpcContext | None = None
    secretsmanager: SecretsManagerContext | None
    region: str
    slug: str
    stage: str
    handlers: dict[str, LambdaHandlerContext]
    repository_name: str
    use_s3: bool
    use_route53: bool
    use_sqs: bool

    @cached_property
    def resource(self):
        return AwsContainer(self)

    @model_validator(mode="before")
    @classmethod
    def context(cls, data: dict) -> dict:
        settings = context_settings.get()
        data["region"] = settings.region
        data["slug"] = settings.slug
        data["stage"] = settings.stage
        data["repository_name"] = (
            settings.object_prefix + settings.project_name + "-lambda"
        )
        data["use_s3"] = settings.s3 is not None
        data["use_route53"] = False
        data["use_sqs"] = False
        for key, handler in data["handlers"].items():
            handler["key"] = key
            if handler["apigateway"]:
                data["use_route53"] = True
            if handler["sqs"]:
                data["use_sqs"] = True
        return data


class NeonContext(settings.Neon):
    project_name: str
    branch_name: str
    name: str
    role_name: str
    region_id: str

    @cached_property
    def resource(self):
        return Neon(self)

    @model_validator(mode="before")
    @classmethod
    def context(cls, data: dict) -> dict:
        settings = context_settings.get()
        data["project_name"] = settings.project_name
        data["branch_name"] = settings.stage
        data["name"] = settings.project_name
        data["role_name"] = settings.project_name
        data["region_id"] = "aws-" + settings.region
        return data


class S3Context(settings.S3):
    region: str
    bucket_name: str

    @cached_property
    def resource(self):
        return S3(self)

    @model_validator(mode="before")
    @classmethod
    def context(cls, data: dict) -> dict:
        settings = context_settings.get()
        data["region"] = settings.region
        data["bucket_name"] = "%s%s" % (
            settings.object_prefix,
            settings.slug,
        )
        return data


class DjangoStorageContext(settings.DjangoStorage):
    @property
    def backend(self):
        if self.store == "s3":
            if self.static and self.manifest:
                return "storages.backends.s3boto3.S3ManifestStaticStorage"
            if self.static:
                return "storages.backends.s3boto3.S3StaticStorage"
            return "storages.backends.s3boto3.S3Boto3Storage"
        raise ValueError("Unknown store")


class DjangoContext(settings.Django):
    storages: dict[str, DjangoStorageContext] = {}


class Context(settings.Settings):
    region: str
    awscontainer: AwsContainerContext | None = None
    neon: NeonContext | None = None
    s3: S3Context | None = None
    django: DjangoContext | None = None

    model_config = SettingsConfigDict(extra="ignore")

    @classmethod
    def from_settings(cls, settings: settings.Settings) -> Context:
        token = context_settings.set(settings)
        try:
            data = settings.model_dump()
            return cls.model_validate(data)
        finally:
            context_settings.reset(token)

    @classmethod
    def from_toml(
        cls, *, stage: str, path: str | Path = Path("pocket.toml"), filters=None
    ):
        return cls.from_settings(
            settings.Settings.from_toml(stage=stage, path=path, filters=filters)
        )

    @model_validator(mode="after")
    def check_django_storage(self):
        if self.django and self.django.storages:
            for storage in self.django.storages.values():
                if storage.store == "s3" and not self.s3:
                    raise ValueError("s3 is required for s3 storage")
        return self
