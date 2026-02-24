from __future__ import annotations

from functools import cached_property
from pathlib import Path

from pydantic import BaseModel, computed_field, model_validator

from . import general_settings
from .django.context import DjangoContext
from .resources.vpc import Vpc as VpcResource
from .utils import get_toml_path


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
            name=gs.namespace + "-" + vpc_ref + "-" + gs.project_name,
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
            name=gs.namespace + "-" + vpc.ref + "-" + gs.project_name,
            region=gs.region,
        )

    @classmethod
    def from_toml(cls, *, ref: str, path: str | Path | None = None) -> VpcContext:
        path = path or get_toml_path()
        gs = general_settings.GeneralSettings.from_toml(path=path)
        for vpc in gs.vpcs:
            if vpc.ref == ref:
                return cls.from_settings(vpc, gs)
        raise ValueError(f"vpc ref [{ref}] not found")


class GeneralContext(BaseModel):
    namespace: str = "pocket"
    prefix_template: str = "{stage}-{project}-{namespace}-"
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
            namespace=gs.namespace,
            prefix_template=gs.prefix_template,
            region=gs.region,
            project_name=gs.project_name,
            stages=gs.stages,
            vpcs=vpcs,
            s3_fallback_bucket_name=gs.s3_fallback_bucket_name,
            django_fallback=django_fallback,
        )

    @classmethod
    def from_toml(cls, *, path: str | Path | None = None):
        path = path or get_toml_path()
        return cls.from_general_settings(
            general_settings.GeneralSettings.from_toml(path=path)
        )

    @model_validator(mode="after")
    def check_django(self):
        assert self.django_fallback, "django_fallback should be set by settings."
        for _, storage in self.django_fallback.storages.items():
            if storage.store == "s3" and not self.s3_fallback_bucket_name:
                raise ValueError(
                    "s3_fallback_bucket_name is required "
                    "to use s3 storage is fallback_context."
                )
        return self
