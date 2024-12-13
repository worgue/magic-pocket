from __future__ import annotations

import sys
from pathlib import Path

from pydantic import model_validator

if sys.version_info >= (3, 11):
    pass
else:
    pass

from ..settings import context_settings
from . import settings


class DjangoStorageContext(settings.DjangoStorage):
    @property
    def backend(self):
        if self.store == "cloudfront":
            return "storages.backends.s3boto3.S3Boto3Storage"
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

    @model_validator(mode="before")
    @classmethod
    def context(cls, data: dict) -> dict:
        if data["store"] == "cloudfront":
            settings = context_settings.get()
            if not settings.cloudfront:
                raise ValueError("cloudfront settings required")
            cloudfront_ref = data["options"]["cloudfront_ref"]
            if cloudfront_ref not in [r.ref for r in settings.cloudfront.routes]:
                raise ValueError("cloudfront ref [%s] not found" % cloudfront_ref)
        return data


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
