from typing import Callable
from urllib.parse import urlencode

from django.utils.encoding import filepath_to_uri
from storages.backends.s3boto3 import (
    S3Boto3Storage,
    S3ManifestStaticStorage,
    S3StaticStorage,
)
from storages.utils import clean_name


class PublicCloudFrontUrlMixin:
    url_protocol: str
    custom_domain: str
    custom_origin_path: str
    _normalize_name: Callable[[str], str]

    def __init__(self, **settings):
        self.custom_origin_path = settings.pop("custom_origin_path", "")
        super().__init__(**settings)

    def _get_url_path(self, name):
        bucket_location = filepath_to_uri(name)
        if not self.custom_origin_path:
            return bucket_location
        if not self.custom_origin_path.startswith("/"):
            raise ValueError("custom_origin_path must start with /")
        custom_origin_path_no_slash = self.custom_origin_path[1:]
        if not bucket_location.startswith(custom_origin_path_no_slash):
            raise ValueError("bucket location mismatch")
        # remove custom_origin_path_no_slash + trailing slash
        # e.g) custom_origin_path == '/dev', bucket_location == 'dev/static'
        #      => return 'static'
        return bucket_location[len(self.custom_origin_path) :]

    def url(self, name, parameters=None, expire=None, http_method=None):
        name = self._normalize_name(clean_name(name))
        params = parameters.copy() if parameters else {}
        if expire:
            raise ValueError("expire is not supported by this storage backend")
        if not self.custom_domain:
            raise ValueError("custom_domain is required for this storage backend")
        url_path = self._get_url_path(name)
        return "{}//{}/{}{}".format(
            self.url_protocol,
            self.custom_domain,
            url_path,
            "?{}".format(urlencode(params)) if params else "",
        )


class PublicCloudFrontS3ManifestStaticStorage(
    PublicCloudFrontUrlMixin, S3ManifestStaticStorage
):
    pass


class PublicCloudFrontS3StaticStorage(PublicCloudFrontUrlMixin, S3StaticStorage):
    pass


class PublicCloudFrontS3Boto3Storage(PublicCloudFrontUrlMixin, S3Boto3Storage):
    pass
