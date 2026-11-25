from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlencode

from django.contrib.staticfiles.storage import ManifestFilesMixin
from django.core.files.storage import Storage
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


class ObjectHashStaticStorage(Storage):
    """`versioning = "object_hash"` の route の staticfiles backend。

    `url(name)` を S3 の release 表 (pocket.static.StaticResolver) から解決するだけ。
    ファイルは `pocket static publish` が上げるので、保存系の操作はできない
    (collectstatic は Lambda では走らせない。ローカル開発は
    `POCKET_STATICFILES_BACKEND_OVERRIDE` で filesystem に切り替える)。
    release 表に無い name は `StaticPathNotFound` (hash なしの URL を黙って返さない)。
    """

    def __init__(self, distribution: str, route: str | None = None, **options):
        self.distribution = distribution
        self.route = route
        self.stage = options.pop("stage", None)
        self.refresh_interval = options.pop("refresh_interval", None)
        self._resolver = None

    @property
    def resolver(self):
        if self._resolver is None:
            from pocket.static import get_static_resolver

            kwargs = {}
            if self.refresh_interval is not None:
                kwargs["refresh_interval"] = self.refresh_interval
            self._resolver = get_static_resolver(
                self.distribution, self.route, stage=self.stage, **kwargs
            )
        return self._resolver

    def url(self, name, *args, **kwargs):
        return self.resolver.url(clean_name(name or ""))

    def exists(self, name):
        try:
            self.resolver.url(clean_name(name or ""))
        except KeyError:
            return False
        return True

    def _open(self, name, mode="rb"):
        raise NotImplementedError(
            "ObjectHashStaticStorage は URL の解決だけを行います (open 不可)"
        )

    def _save(self, name, content):
        raise NotImplementedError(
            "ObjectHashStaticStorage には保存できません。"
            "静的ファイルは `pocket static publish` で上げてください"
        )

    def delete(self, name):
        raise NotImplementedError("ObjectHashStaticStorage は削除できません")

    def listdir(self, path):
        raise NotImplementedError("ObjectHashStaticStorage は一覧できません")

    def size(self, name):
        raise NotImplementedError("ObjectHashStaticStorage は size を返せません")
