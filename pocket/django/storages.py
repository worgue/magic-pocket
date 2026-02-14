from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from django.utils.encoding import filepath_to_uri
from storages.backends.s3boto3 import (
    S3Boto3Storage,
    S3ManifestStaticStorage,
    S3StaticStorage,
)
from storages.utils import clean_name


class PocketStorageConfigurationError(Exception):
    pass


class CloudFrontOriginPathMixin:
    url_protocol: str
    custom_domain: str
    querystring_expire: int
    querystring_auth: bool
    cloudfront_signer: Any

    def __init__(self, **settings):
        self.custom_origin_path = settings.pop("custom_origin_path", "")
        super().__init__(**settings)
        if not self.querystring_auth and self.cloudfront_signer:
            raise PocketStorageConfigurationError(
                "cloudfront_signer can only be used with querystring_auth"
            )

    def get_url_with_custom_origin_path(self, url):
        current_prefix = "{}//{}{}".format(
            self.url_protocol, self.custom_domain, self.custom_origin_path
        )
        desired_prefix = "{}//{}".format(self.url_protocol, self.custom_domain)
        return url.replace(current_prefix, desired_prefix)


class CloudFrontOriginPathStaticMixin(CloudFrontOriginPathMixin):
    def __init__(self, **settings):
        super().__init__(**settings)
        if self.cloudfront_signer:
            raise PocketStorageConfigurationError(
                "cloudfront_signer can not used with static files"
            )

    def url(self, *args, **kwargs):
        url = super().url(*args, **kwargs)  # type: ignore
        if self.custom_domain and self.custom_origin_path:
            url = self.get_url_with_custom_origin_path(url)
        return url


class CloudFrontS3ManifestStaticStorage(
    CloudFrontOriginPathStaticMixin, S3ManifestStaticStorage
):
    pass


class CloudFrontS3StaticStorage(CloudFrontOriginPathStaticMixin, S3StaticStorage):
    pass


class CloudFrontS3Boto3Storage(CloudFrontOriginPathMixin, S3Boto3Storage):
    def url(self, name, parameters=None, expire=None, http_method=None):
        if not (self.custom_domain and self.custom_origin_path):
            return super().url(name, parameters, expire, http_method)  # type: ignore

        # Copy from S3Storage
        name = self._normalize_name(clean_name(name))  # type: ignore
        params = parameters.copy() if parameters else {}
        if expire is None:
            expire = self.querystring_expire

        url = "{}//{}/{}{}".format(
            self.url_protocol,
            self.custom_domain,
            filepath_to_uri(name),
            "?{}".format(urlencode(params)) if params else "",
        )

        # This class only change the URL
        url = self.get_url_with_custom_origin_path(url)

        # Copy from S3Storage
        if self.querystring_auth and self.cloudfront_signer:
            expiration = datetime.utcnow() + timedelta(seconds=expire)
            return self.cloudfront_signer.generate_presigned_url(
                url, date_less_than=expiration
            )

        return url
