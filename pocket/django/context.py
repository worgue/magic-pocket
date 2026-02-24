from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from . import settings


class DjangoStorageContext(BaseModel):
    store: Literal["s3", "cloudfront", "filesystem"]
    location: str | None = None
    static: bool = False
    manifest: bool = False
    options: dict[str, Any] = {}

    @property
    def backend(self):
        if self.store == "s3":
            if self.static and self.manifest:
                return "storages.backends.s3boto3.S3ManifestStaticStorage"
            if self.static:
                return "storages.backends.s3boto3.S3StaticStorage"
            return "storages.backends.s3boto3.S3Boto3Storage"
        elif self.store == "cloudfront":
            if self.static and self.manifest:
                return "pocket.django.storages.CloudFrontS3ManifestStaticStorage"
            if self.static:
                return "pocket.django.storages.CloudFrontS3StaticStorage"
            return "pocket.django.storages.CloudFrontS3Boto3Storage"
        elif self.store == "filesystem":
            if self.static and self.manifest:
                return "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"
            if self.static:
                return "django.contrib.staticfiles.storage.StaticFilesStorage"
            return "django.core.files.storage.FileSystemStorage"
        raise ValueError("Unknown store")

    @classmethod
    def from_settings(
        cls, storage: settings.DjangoStorage, *, cloudfront_routes=None
    ) -> DjangoStorageContext:
        if storage.store == "cloudfront":
            if cloudfront_routes is None:
                raise ValueError("cloudfront settings required")
            cloudfront_ref = storage.options["cloudfront_ref"]
            if cloudfront_ref not in [r.ref for r in cloudfront_routes]:
                raise ValueError("cloudfront ref [%s] not found" % cloudfront_ref)
        return cls(
            store=storage.store,
            location=storage.location,
            static=storage.static,
            manifest=storage.manifest,
            options=storage.options,
        )


class DjangoCacheContext(BaseModel):
    store: Literal["efs", "locmem"]
    location_subdir: str = "{stage}"
    location: str | None = None

    @property
    def backend(self):
        if self.store == "efs":
            return "django.core.cache.backends.filebased.FileBasedCache"
        elif self.store == "locmem":
            return "django.core.cache.backends.locmem.LocMemCache"
        raise ValueError("Unknown store")

    @classmethod
    def from_settings(
        cls, cache: settings.DjangoCache, *, root=None
    ) -> DjangoCacheContext:
        location = None
        if cache.store == "locmem":
            pass
        elif cache.store == "efs":
            assert (
                root
                and root.awscontainer
                and root.awscontainer.vpc
                and root.awscontainer.vpc.efs
            )
            format_vars = {
                "namespace": root.namespace,
                "stage": root.stage,
                "project": root.project_name,
            }
            mnt = Path(root.awscontainer.vpc.efs.local_mount_path)
            location = str(mnt / cache.location_subdir).format(**format_vars)
        return cls(
            store=cache.store,
            location_subdir=cache.location_subdir,
            location=location,
        )


class DjangoContext(BaseModel):
    storages: dict[str, DjangoStorageContext] = {}
    caches: dict[str, DjangoCacheContext] = {}
    settings: dict[str, Any] = {}

    @classmethod
    def from_settings(cls, django: settings.Django, *, root=None) -> DjangoContext:
        cloudfront_routes = None
        if root and root.cloudfront:
            cloudfront_routes = root.cloudfront.routes

        storages = {}
        for key, storage in (django.storages or {}).items():
            storages[key] = DjangoStorageContext.from_settings(
                storage, cloudfront_routes=cloudfront_routes
            )

        caches = {}
        for key, cache in (django.caches or {}).items():
            caches[key] = DjangoCacheContext.from_settings(cache, root=root)

        return cls(
            storages=storages,
            caches=caches,
            settings=django.settings,
        )
