from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from . import settings

_STATIC_FILES_STORAGE = "django.contrib.staticfiles.storage.StaticFilesStorage"
_MANIFEST_STATIC_FILES_STORAGE = (
    "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"
)


class DjangoStorageContext(BaseModel):
    store: Literal["s3", "filesystem"]
    location: str | None = None
    static: bool = False
    manifest: bool = False
    options: dict[str, Any] = {}
    distribution: str | None = None
    route: str | None = None
    deploy_hash: bool = False
    publish: settings.PublishMode = "deploy"

    @property
    def backend(self):
        # deploy_hash: STATIC_URL ベースで URL を生成
        if self.deploy_hash and self.static:
            return _STATIC_FILES_STORAGE
        if self.store == "s3":
            return self._s3_backend
        if self.store == "filesystem":
            return self._filesystem_backend
        raise ValueError("Unknown store")

    @property
    def _s3_backend(self) -> str:
        if self.distribution:
            if self.static and self.manifest:
                return "pocket.django.storages.CloudFrontS3ManifestStaticStorage"
            if self.static:
                return "pocket.django.storages.CloudFrontS3StaticStorage"
            return "pocket.django.storages.CloudFrontS3Boto3Storage"
        if self.static and self.manifest:
            return "storages.backends.s3boto3.S3ManifestStaticStorage"
        if self.static:
            return "storages.backends.s3boto3.S3StaticStorage"
        return "storages.backends.s3boto3.S3Boto3Storage"

    @property
    def _filesystem_backend(self) -> str:
        if self.static and self.manifest:
            return _MANIFEST_STATIC_FILES_STORAGE
        if self.static:
            return _STATIC_FILES_STORAGE
        return "django.core.files.storage.FileSystemStorage"

    @classmethod
    def from_settings(
        cls,
        storage: settings.DjangoStorage,
        *,
        cloudfront_distributions: dict | None = None,
    ) -> DjangoStorageContext:
        if storage.distribution and cloudfront_distributions:
            if storage.distribution not in cloudfront_distributions:
                raise ValueError(
                    "distribution '%s' not found in cloudfront" % storage.distribution
                )
            cf = cloudfront_distributions[storage.distribution]
            if storage.route:
                route_refs = [r.ref for r in cf.routes]
                if storage.route not in route_refs:
                    raise ValueError(
                        "route ref '%s' not found in cloudfront.%s"
                        % (storage.route, storage.distribution)
                    )
        # route の versioning が deploy_hash かどうかを判定
        is_deploy_hash = False
        if storage.distribution and storage.route and cloudfront_distributions:
            cf = cloudfront_distributions.get(storage.distribution)
            if cf:
                for r in cf.routes:
                    if r.ref == storage.route and r.versioning == "deploy_hash":
                        is_deploy_hash = True
                        break
        return cls(
            store=storage.store,
            location=storage.location,
            static=storage.static,
            manifest=storage.manifest,
            options=storage.options,
            distribution=storage.distribution,
            route=storage.route,
            deploy_hash=is_deploy_hash,
            publish=storage.publish,
        )


class DjangoCacheContext(BaseModel):
    store: Literal["efs", "locmem", "redis"]
    location_subdir: str = "{stage}"
    location: str | None = None

    @property
    def backend(self):
        if self.store == "efs":
            return "django.core.cache.backends.filebased.FileBasedCache"
        elif self.store == "locmem":
            return "django.core.cache.backends.locmem.LocMemCache"
        elif self.store == "redis":
            return "django_redis.cache.RedisCache"
        raise ValueError("Unknown store")

    @classmethod
    def from_settings(
        cls, cache: settings.DjangoCache, *, root=None
    ) -> DjangoCacheContext:
        location = None
        if cache.store == "locmem":
            pass
        elif cache.store == "redis":
            pass
        elif cache.store == "efs":
            if not (
                root
                and root.awscontainer
                and root.awscontainer.vpc
                and root.awscontainer.vpc.efs
            ):
                raise RuntimeError("efs cache requires awscontainer.vpc.efs")
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
    project_dir: str | None = None

    @classmethod
    def from_settings(cls, django: settings.Django, *, root=None) -> DjangoContext:
        cloudfront_distributions = None
        if root and root.cloudfront:
            cloudfront_distributions = root.cloudfront

        storages = {}
        for key, storage in (django.storages or {}).items():
            storages[key] = DjangoStorageContext.from_settings(
                storage, cloudfront_distributions=cloudfront_distributions
            )

        caches = {}
        for key, cache in (django.caches or {}).items():
            caches[key] = DjangoCacheContext.from_settings(cache, root=root)

        return cls(
            storages=storages,
            caches=caches,
            settings=django.settings,
            project_dir=django.project_dir,
        )
