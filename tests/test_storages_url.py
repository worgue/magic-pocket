"""CloudFront origin_path 付き storage の URL 生成順序のテスト。

署名は origin_path 除去後の URL に対して行う必要がある (署名後に URL を
書き換えると署名対象と実 URL が食い違い、CloudFront が 403 を返す)。
"""

from __future__ import annotations

import django
from django.conf import settings as dj_settings

if not dj_settings.configured:
    dj_settings.configure(
        DEFAULT_CHARSET="utf-8",
        USE_TZ=True,
        INSTALLED_APPS=["django.contrib.staticfiles"],
        STATIC_URL="/static/",
    )
    django.setup()

from django.contrib.staticfiles.storage import ManifestFilesMixin  # noqa: E402

from pocket.django.storages import (  # noqa: E402
    CloudFrontOriginPathMixin,
    CloudFrontS3Boto3Storage,
    CloudFrontS3ManifestStaticStorage,
    CloudFrontS3StaticStorage,
)


class _FakeSigner:
    def __init__(self):
        self.signed_urls: list[str] = []

    def generate_presigned_url(self, url, date_less_than=None):
        self.signed_urls.append(url)
        return url + "?Signature=fake"


def _make_static_storage(signer):
    return CloudFrontS3StaticStorage(
        bucket_name="test-bucket",
        location="static",
        custom_domain="cdn.example.com",
        custom_origin_path="/static",
        querystring_auth=signer is not None,
        cloudfront_signer=signer,
    )


def test_signed_static_url_signs_after_origin_path_strip():
    """署名が origin_path 除去後の URL に対して行われること"""
    signer = _FakeSigner()
    storage = _make_static_storage(signer)
    url = storage.url("css/app.css")
    assert signer.signed_urls == ["https://cdn.example.com/css/app.css"]
    assert url == "https://cdn.example.com/css/app.css?Signature=fake"


def test_unsigned_static_url_strips_origin_path():
    """非署名でも origin_path が除去された URL を返すこと"""
    storage = _make_static_storage(None)
    assert storage.url("css/app.css") == "https://cdn.example.com/css/app.css"


def test_signed_media_url_signs_after_origin_path_strip():
    """非 static (media) 側も同順であること"""
    signer = _FakeSigner()
    storage = CloudFrontS3Boto3Storage(
        bucket_name="test-bucket",
        location="media",
        custom_domain="cdn.example.com",
        custom_origin_path="/media",
        querystring_auth=True,
        cloudfront_signer=signer,
    )
    url = storage.url("uploads/photo.jpg")
    assert signer.signed_urls == ["https://cdn.example.com/uploads/photo.jpg"]
    assert url.endswith("?Signature=fake")


def test_manifest_static_resolves_hash_before_cloudfront_url():
    """manifest storage は MRO でハッシュ名解決が origin_path 処理より先であること"""
    mro = CloudFrontS3ManifestStaticStorage.__mro__
    assert mro.index(ManifestFilesMixin) < mro.index(CloudFrontOriginPathMixin)
