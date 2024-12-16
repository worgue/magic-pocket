from storages.backends.s3boto3 import (
    S3Boto3Storage,
    S3ManifestStaticStorage,
    S3StaticStorage,
)


class CloudFrontOriginPathMixin:
    url_protocol: str
    custom_domain: str

    def __init__(self, **settings):
        self.custom_origin_path = settings.pop("custom_origin_path", "")
        super().__init__(**settings)

    def url(self, name, force=False):
        super_url = super().url(name, force)  # type: ignore
        current_prefix = "{}//{}{}".format(
            self.url_protocol, self.custom_domain, self.custom_origin_path
        )
        desired_prefix = "{}//{}".format(self.url_protocol, self.custom_domain)
        return super_url.replace(current_prefix, desired_prefix)


class CloudFrontS3ManifestStaticStorage(
    CloudFrontOriginPathMixin, S3ManifestStaticStorage
):
    pass


class CloudFrontS3StaticStorage(CloudFrontOriginPathMixin, S3StaticStorage):
    pass


class CloudFrontS3Boto3Storage(CloudFrontOriginPathMixin, S3Boto3Storage):
    pass
