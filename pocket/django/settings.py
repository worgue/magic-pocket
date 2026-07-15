from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

# staticfiles の publish 方式 (DB/KVS の ProvisioningMode と同じ思想の静的版)。
#   "deploy"  : deploy / promote が collectstatic + upload を実行する (zero-config)。
#   "command" : deploy / promote は静的に一切触れない。publish は
#               `pocket django deploystatic` に一任する (静的を out-of-band
#               管理する project 用。publish 経路を CI と分離できる)。
PublishMode = Literal["deploy", "command"]


class DjangoStorage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store: Literal["s3", "filesystem"]
    location: str | None = None
    static: bool = False
    manifest: bool = False
    options: dict[str, Any] = {}
    distribution: str | None = None
    route: str | None = None
    publish: PublishMode = "deploy"

    @model_validator(mode="after")
    def check_manifest(self):
        if self.manifest and not self.static:
            raise ValueError("manifest can only be used with static")
        return self

    @model_validator(mode="after")
    def check_location(self):
        if self.store == "s3" and self.location is None and not self.distribution:
            raise ValueError("location is required for s3 storage without distribution")
        if self.distribution and self.location is not None:
            raise ValueError(
                "location cannot be used with distribution "
                "(S3 location is computed from the route's origin_path)"
            )
        return self

    @model_validator(mode="after")
    def check_route(self):
        if self.route and not self.distribution:
            raise ValueError("route requires distribution")
        return self

    @model_validator(mode="after")
    def check_distribution(self):
        if self.distribution and self.store != "s3":
            raise ValueError("distribution can only be used with s3 store")
        return self

    @model_validator(mode="after")
    def check_publish(self):
        if self.publish != "deploy" and not self.static:
            raise ValueError("publish can only be used with static storage")
        return self


class DjangoCache(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store: Literal["efs", "locmem", "redis"]
    location_subdir: str = "{stage}"


class Django(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storages: dict[str, DjangoStorage] | None = None
    caches: dict[str, DjangoCache] | None = None
    settings: dict[str, Any] = {}
    project_dir: str | None = None

    @model_validator(mode="after")
    def set_defaults(self):
        if self.storages is None:
            # https://docs.djangoproject.com/en/5.0/ref/settings/#storages
            self.storages = {
                "default": DjangoStorage(store="filesystem"),
                "staticfiles": DjangoStorage(store="filesystem", static=True),
            }
        if "staticfiles" not in self.storages:
            raise ValueError("staticfiles storage is required")
        if "default" not in self.storages:
            raise ValueError("default storage is required")
        if not self.storages["staticfiles"].static:
            raise ValueError("staticfiles storage must be static")
        for key, s in self.storages.items():
            if key != "staticfiles" and s.static:
                raise ValueError("static can only be used with staticfiles")
        if self.caches is None:
            # https://docs.djangoproject.com/en/5.0/ref/settings/#caches
            self.caches = self.caches or {"default": DjangoCache(store="locmem")}
        return self
