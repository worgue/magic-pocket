from __future__ import annotations

from contextvars import ContextVar
from functools import cached_property
from pathlib import Path

from pydantic import BaseModel, computed_field, model_validator

from . import general_settings
from .django.context import DjangoContext
from .resources.vpc import Vpc as VpcResource
from .utils import get_toml_path

context_general_settings = general_settings.context_general_settings
context_vpcref: ContextVar[VpcRefContext] = ContextVar("vpcref")


class GeneralContext(general_settings.GeneralSettings):
    vpcs: list[VpcContext] = []
    django_fallback: DjangoContext | None = None
    django_test: DjangoContext | None = None

    @classmethod
    def from_general_settings(
        cls, general_settings: general_settings.GeneralSettings
    ) -> GeneralContext:
        token = context_general_settings.set(general_settings)
        try:
            data = general_settings.model_dump(by_alias=True)
            return cls.model_validate(data)
        finally:
            context_general_settings.reset(token)

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


class EfsContext(general_settings.Efs):
    name: str
    region: str

    @model_validator(mode="before")
    @classmethod
    def context(cls, data: dict) -> dict:
        gs = context_general_settings.get()
        vrc = context_vpcref.get()
        data["name"] = gs.object_prefix + vrc.ref + "-" + gs.project_name
        data["region"] = gs.region
        return data


class VpcRefContext(BaseModel):
    ref: str


class VpcContext(general_settings.Vpc):
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
        gs = context_general_settings.get()
        data["region"] = gs.region
        data["name"] = gs.object_prefix + data["ref"] + "-" + gs.project_name
        return data

    @model_validator(mode="wrap")
    @classmethod
    def validate_model(cls, v, handler):
        vrc = VpcRefContext(ref=v["ref"])
        token = context_vpcref.set(vrc)
        try:
            return handler(v)
        finally:
            context_vpcref.reset(token)

    @cached_property
    def resource(self):
        return VpcResource(self)
