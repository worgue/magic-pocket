from __future__ import annotations

from functools import cached_property

from pydantic import BaseModel, computed_field, model_validator

from . import general_settings
from .django.context import DjangoContext
from .resources.vpc import Vpc as VpcResource


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
            name=gs.object_prefix + vpc_ref + "-" + gs.project_name,
            region=gs.region,
        )


class VpcContext(BaseModel):
    ref: str
    zone_suffixes: list[str] = ["a"]
    nat_gateway: bool = True
    internet_gateway: bool = True
    efs: EfsContext | None = None
    name: str
    region: str

    @computed_field
    @property
    def private_route_table(self) -> bool:
        return self.nat_gateway

    @computed_field
    @property
    def zones(self) -> list[str]:
        return [f"{self.region}{suffix}" for suffix in self.zone_suffixes]

    @cached_property
    def resource(self):
        return VpcResource(self)

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
            name=gs.object_prefix + vpc.ref + "-" + gs.project_name,
            region=gs.region,
        )

    @classmethod
    def from_toml(cls, *, ref: str) -> VpcContext:
        gs = general_settings.GeneralSettings.from_toml()
        for vpc in gs.vpcs:
            if vpc.ref == ref:
                return cls.from_settings(vpc, gs)
        raise ValueError(f"vpc ref [{ref}] not found")


class GeneralContext(BaseModel):
    object_prefix: str = "pocket-"
    region: str
    project_name: str
    stages: list[str]
    vpcs: list[VpcContext] = []
    s3_fallback_bucket_name: str | None = None
    django_fallback: DjangoContext | None = None

    @classmethod
    def from_general_settings(
        cls, gs: general_settings.GeneralSettings
    ) -> GeneralContext:
        vpcs = [VpcContext.from_settings(vpc, gs) for vpc in gs.vpcs]
        django_fallback = None
        if gs.django_fallback:
            django_fallback = DjangoContext.from_settings(gs.django_fallback)
        return cls(
            object_prefix=gs.object_prefix,
            region=gs.region,
            project_name=gs.project_name,
            stages=gs.stages,
            vpcs=vpcs,
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
