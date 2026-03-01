from __future__ import annotations

import sys

from pydantic import BaseModel, computed_field, model_validator

from . import general_settings
from .django.context import DjangoContext
from .utils import get_toml_path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


class EfsContext(BaseModel):
    local_mount_path: str = "/mnt/efs"
    access_point_path: str = "/lambda"
    name: str
    region: str

    @classmethod
    def from_settings(
        cls,
        efs: general_settings.Efs,
        gs: general_settings.GeneralSettings,
        vpc_ref: str,
    ) -> EfsContext:
        return cls(
            local_mount_path=efs.local_mount_path,
            access_point_path=efs.access_point_path,
            name=vpc_ref + "-" + gs.namespace,
            region=gs.region,
        )


class VpcContext(BaseModel):
    ref: str
    zone_suffixes: list[str] = []
    nat_gateway: bool = True
    internet_gateway: bool = True
    efs: EfsContext | None = None
    name: str
    region: str
    manage: bool = True
    sharable: bool = False

    @computed_field
    @property
    def private_route_table(self) -> bool:
        return self.nat_gateway

    @computed_field
    @property
    def zones(self) -> list[str]:
        return [f"{self.region}{suffix}" for suffix in self.zone_suffixes]

    @classmethod
    def from_settings(
        cls,
        vpc: general_settings.Vpc,
        gs: general_settings.GeneralSettings,
    ) -> VpcContext:
        efs_ctx = None
        if vpc.efs:
            efs_ctx = EfsContext.from_settings(vpc.efs, gs, vpc.ref)
        return cls(
            ref=vpc.ref,
            zone_suffixes=vpc.zone_suffixes,
            nat_gateway=vpc.nat_gateway,
            internet_gateway=vpc.internet_gateway,
            efs=efs_ctx,
            name=vpc.ref + "-" + gs.namespace,
            region=gs.region,
            manage=vpc.manage,
            sharable=vpc.sharable,
        )

    @classmethod
    def from_toml(cls) -> VpcContext:
        gs = general_settings.GeneralSettings.from_toml()
        data = tomllib.loads(get_toml_path().read_text())
        vpc_data = data.get("vpc")
        if not vpc_data:
            raise ValueError("[vpc] が定義されていません")
        vpc = general_settings.Vpc.model_validate(vpc_data)
        return cls.from_settings(vpc, gs)


class GeneralContext(BaseModel):
    namespace: str = "pocket"
    prefix_template: str = "{stage}-{project}-{namespace}-"
    region: str
    project_name: str
    stages: list[str]
    s3_fallback_bucket_name: str | None = None
    django_fallback: DjangoContext | None = None

    @classmethod
    def from_general_settings(
        cls, gs: general_settings.GeneralSettings
    ) -> GeneralContext:
        django_fallback = None
        if gs.django_fallback:
            django_fallback = DjangoContext.from_settings(gs.django_fallback)
        return cls(
            namespace=gs.namespace,
            prefix_template=gs.prefix_template,
            region=gs.region,
            project_name=gs.project_name,
            stages=gs.stages,
            s3_fallback_bucket_name=gs.s3_fallback_bucket_name,
            django_fallback=django_fallback,
        )

    @classmethod
    def from_toml(cls):
        return cls.from_general_settings(general_settings.GeneralSettings.from_toml())

    @model_validator(mode="after")
    def check_django(self):
        assert self.django_fallback, "django_fallback should be set by settings."
        for _, storage in self.django_fallback.storages.items():
            if storage.store == "s3" and not self.s3_fallback_bucket_name:
                raise ValueError(
                    "S3 storage is configured in [general.django.storages] "
                    "but s3_fallback_bucket_name is not set in [general]. "
                    "Either add s3_fallback_bucket_name to [general] for local "
                    "development, or set POCKET_STAGE environment variable "
                    "to use a stage-specific S3 bucket."
                )
        return self
