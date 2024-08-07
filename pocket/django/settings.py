from __future__ import annotations

from typing import Any, Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings


class DjangoStorage(BaseSettings):
    store: Literal["s3", "filesystem"]
    location: str | None = None
    static: bool = False
    manifest: bool = False
    options: dict[str, Any] = {}

    @model_validator(mode="after")
    def check_manifest(self):
        if self.manifest and not self.static:
            raise ValueError("manifest can only be used with static")
        return self

    @model_validator(mode="after")
    def check_location(self):
        if self.store == "s3" and self.location is None:
            raise ValueError("location is required for s3 storage")
        return self


class DjangoCache(BaseSettings):
    store: Literal["efs", "locmem"]
    location_subdir: str = "{stage}"


class Django(BaseSettings):
    storages: dict[str, DjangoStorage] | None = None
    caches: dict[str, DjangoCache] | None = None
    settings: dict[str, Any] = {}

    @model_validator(mode="after")
    def set_defaults(self):
        if self.storages is None:
            # https://docs.djangoproject.com/en/5.0/ref/settings/#storages
            self.storages = {
                "default": DjangoStorage(store="filesystem"),
                "staticfiles": DjangoStorage(store="filesystem", static=True),
            }
        if self.caches is None:
            # https://docs.djangoproject.com/en/5.0/ref/settings/#caches
            self.caches = self.caches or {"default": DjangoCache(store="locmem")}
        return self
