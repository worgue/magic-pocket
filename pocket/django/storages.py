from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlencode

from django.contrib.staticfiles.storage import ManifestFilesMixin
from django.utils.encoding import filepath_to_uri
from storages.backends.s3boto3 import (
    S3Boto3Storage,
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
    _normalize_name: Callable[[str], str]

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

    def url(self, name, *args, **kwargs):
        # シグネチャは base によって異なる (S3Storage: parameters/expire/http_method、
        # ManifestFilesMixin: force) ため可変引数で受ける
        if not (self.custom_domain and self.custom_origin_path):
            return super().url(name, *args, **kwargs)  # type: ignore

        # 「URL 構築 → origin_path 除去 → 署名」の順を守る。署名後に URL を
        # 書き換えると署名対象と実 URL が食い違い、CloudFront が 403 を返す。
        parameters = kwargs.get("parameters", args[0] if len(args) >= 1 else None)
        expire = kwargs.get("expire", args[1] if len(args) >= 2 else None)

        # Copy from S3Storage
        name = self._normalize_name(clean_name(name))
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


class CloudFrontS3Boto3Storage(CloudFrontOriginPathMixin, S3Boto3Storage):
    pass


class CloudFrontS3StaticStorage(CloudFrontOriginPathMixin, S3StaticStorage):
    pass


class CloudFrontS3ManifestStaticStorage(ManifestFilesMixin, CloudFrontS3StaticStorage):
    # ManifestFilesMixin を MRO の先頭に置くことで、ハッシュ名解決 →
    # CloudFrontOriginPathMixin.url (origin_path 除去 → 署名) の順になる
    pass
