from __future__ import annotations

import sys
from contextvars import ContextVar
from functools import cached_property
from pathlib import Path

from pydantic import BaseModel, computed_field, model_validator
from pydantic_settings import SettingsConfigDict

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib
from . import settings
from .resources.aws.secretsmanager import PocketSecretIsNotReady, SecretsManager
from .resources.awscontainer import AwsContainer
from .resources.neon import Neon
from .resources.s3 import S3
from .resources.vpc import Vpc
from .utils import echo, get_hosted_zone_id_from_domain, get_project_name, get_toml_path

context_settings: ContextVar[settings.Settings] = ContextVar("context_settings")
context_vpcvalidate: ContextVar[VpcValidateContext] = ContextVar("context_vpcvalidate")


class EfsContext(settings.Efs):
    name: str
    region: str

    @model_validator(mode="before")
    @classmethod
    def context(cls, data: dict) -> dict:
        vvc = context_vpcvalidate.get()
        data["name"] = vvc.object_prefix + vvc.ref + "-" + vvc.project_name
        data["region"] = vvc.region
        return data


class VpcValidateContext(BaseModel):
    object_prefix: str
    project_name: str
    ref: str
    region: str


class VpcContext(settings.Vpc):
    ref: str
    name: str
    region: str
    efs: EfsContext | None = None

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
        data["name"] = (
            settings.object_prefix + data["ref"] + "-" + settings.project_name
        )
        return data

    @model_validator(mode="wrap")
    @classmethod
    def validate_model(cls, v, handler):
        settings = context_settings.get()
        vvc = VpcValidateContext.model_validate(
            {
                "object_prefix": settings.object_prefix,
                "project_name": settings.project_name,
                "ref": v["ref"],
                "region": settings.region,
            }
        )
        token = context_vpcvalidate.set(vvc)
        try:
            return handler(v)
        finally:
            context_vpcvalidate.reset(token)

    @classmethod
    def from_toml(cls, *, ref: str, path: str | Path | None = None):
        path = path or get_toml_path()
        data = tomllib.loads(Path(path).read_text())
        if "vpcs" not in data:
            raise ValueError("vpcs is required when vpc_ref is used")
        if ref not in data["vpcs"]:
            raise ValueError(f"vpc {ref} not found in vpcs")

        vpc_data = data.get("vpcs", {}).get(ref)
        vpc_data["ref"] = ref

        class MockContext:
            region = data["region"]
            project_name = data.get("project_name") or get_project_name()

        token = context_settings.set(MockContext)  # pyright: ignore
        try:
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
    apigateway: ApiGatewayContext | None = None
    sqs: SqsContext | None = None
    key: str
    function_name: str
    log_group_name: str

    @model_validator(mode="before")
    @classmethod
    def context(cls, data: dict) -> dict:
        settings = context_settings.get()
        data["region"] = settings.region
        data["function_name"] = f"{settings.object_prefix}{settings.slug}-{data['key']}"
        data["log_group_name"] = f"/aws/lambda/{data['function_name']}"
        if data["sqs"]:
            data["sqs"]["name"] = f"{settings.slug}-{data['key']}"
            data["sqs"]["visibility_timeout"] = data["timeout"] * 6
        return data


class SecretsManagerContext(settings.SecretsManager):
    region: str
    pocket_key: str
    stage: str
    project_name: str

    @cached_property
    def resource(self):
        return SecretsManager(self)

    def _ensure_arn(self, resource: str):
        if resource.startswith("arn:"):
            return resource
        return (
            "arn:aws:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:" + resource
        )

    @computed_field
    @cached_property
    def allowed_resources(self) -> list[str]:
        resources = list(self.secrets.values())
        if self.pocket_secrets:
            try:
                resources.append(self.resource.pocket_secrets_arn)
            except PocketSecretIsNotReady:
                echo.log(
                    "Pocket managed secrets is not ready. "
                    "The context is not complete data."
                )
        resources += self.extra_resources
        return [self._ensure_arn(resource) for resource in resources if resource]

    @model_validator(mode="before")
    @classmethod
    def context(cls, data: dict) -> dict:
        settings = context_settings.get()
        data["region"] = settings.region
        format_vars = {
            "prefix": settings.object_prefix,
            "stage": settings.stage,
            "project": settings.project_name,
        }
        data["pocket_key"] = data["pocket_key_format"].format(**format_vars)
        data["stage"] = settings.stage
        data["project_name"] = settings.project_name
        return data

    @model_validator(mode="after")
    def check_entry(self):
        if (not self.require_list_secrets) and (not self.allowed_resources):
            echo.log(
                "No secret resouces are associated to lambda."
                "The data is for reference only."
            )
        return self


class DjangoStorageContext(settings.DjangoStorage):
    @property
    def backend(self):
        if self.store == "s3":
            if self.static and self.manifest:
                return "storages.backends.s3boto3.S3ManifestStaticStorage"
            if self.static:
                return "storages.backends.s3boto3.S3StaticStorage"
            return "storages.backends.s3boto3.S3Boto3Storage"
        elif self.store == "filesystem":
            if self.static and self.manifest:
                return "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"
            if self.static:
                return "django.contrib.staticfiles.storage.StaticFilesStorage"
            return "django.core.files.storage.FileSystemStorage"
        raise ValueError("Unknown store")


class DjangoCacheContext(settings.DjangoCache):
    location: str | None

    @property
    def backend(self):
        if self.store == "efs":
            return "django.core.cache.backends.filebased.FileBasedCache"
        elif self.store == "locmem":
            return "django.core.cache.backends.locmem.LocMemCache"
        raise ValueError("Unknown store")

    @model_validator(mode="before")
    @classmethod
    def context(cls, data: dict) -> dict:
        if data["store"] == "locmem":
            data["location"] = None
        elif data["store"] == "efs":
            settings = context_settings.get()
            format_vars = {
                "prefix": settings.object_prefix,
                "stage": settings.stage,
                "project": settings.project_name,
            }
            assert (
                settings.awscontainer
                and settings.awscontainer.vpc
                and settings.awscontainer.vpc.efs
            )
            mnt = Path(settings.awscontainer.vpc.efs.local_mount_path)
            data["location"] = str(mnt / data["location_subdir"]).format(**format_vars)
        return data


class DjangoContext(settings.Django):
    storages: dict[str, DjangoStorageContext] = {}
    caches: dict[str, DjangoCacheContext] = {}


class AwsContainerContext(settings.AwsContainer):
    vpc: VpcContext | None = None
    secretsmanager: SecretsManagerContext | None = None
    region: str
    slug: str
    stage: str
    handlers: dict[str, LambdaHandlerContext] = {}
    ecr_name: str
    use_s3: bool
    use_route53: bool = False
    use_sqs: bool = False
    use_efs: bool = False
    efs_local_mount_path: str = ""
    django: DjangoContext | None = None

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
        data["ecr_name"] = settings.object_prefix + settings.project_name + "-lambda"
        data["use_s3"] = settings.s3 is not None
        if data["vpc"] and (data["vpc"]["efs"] is not None):
            data["use_efs"] = True
            data["efs_local_mount_path"] = data["vpc"]["efs"]["local_mount_path"]
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
        format_vars = {
            "prefix": settings.object_prefix,
            "stage": settings.stage,
            "project": settings.project_name,
        }
        data["bucket_name"] = data["bucket_name_format"].format(**format_vars)
        return data


class Context(settings.Settings):
    region: str
    awscontainer: AwsContainerContext | None = None
    neon: NeonContext | None = None
    s3: S3Context | None = None

    model_config = SettingsConfigDict(extra="ignore")

    @classmethod
    def from_settings(cls, settings: settings.Settings) -> Context:
        token = context_settings.set(settings)
        try:
            data = settings.model_dump(by_alias=True)
            return cls.model_validate(data)
        finally:
            context_settings.reset(token)

    @classmethod
    def from_toml(cls, *, stage: str, path: str | Path | None = None):
        path = path or get_toml_path()
        return cls.from_settings(settings.Settings.from_toml(stage=stage, path=path))

    @model_validator(mode="after")
    def check_django(self):
        if self.awscontainer and self.awscontainer.django:
            for _, storage in self.awscontainer.django.storages.items():
                if storage.store == "s3" and not self.s3:
                    raise ValueError("s3 is required for s3 storage")
            for _, cache in self.awscontainer.django.caches.items():
                if cache.store == "efs" and not (
                    self.awscontainer.vpc and self.awscontainer.vpc.efs
                ):
                    raise ValueError("vpc is required for efs cache")
        return self
