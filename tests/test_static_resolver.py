"""runtime の static_url (StaticResolver) と Django の ObjectHashStaticStorage。"""

from __future__ import annotations

import json

import boto3
import django
import pytest
from django.conf import settings as dj_settings
from moto import mock_aws

from pocket.static import StaticPathNotFound, StaticResolver, get_static_resolver
from pocket.static import resolver as resolver_module
from pocket.static.mapfile import MapEntry, StaticMap
from pocket.static.release import (
    build_current_document,
    build_release_document,
    current_key,
    encode_current,
    encode_release,
    release_key,
)

if not dj_settings.configured:
    dj_settings.configure(
        DEFAULT_CHARSET="utf-8",
        USE_TZ=True,
        INSTALLED_APPS=["django.contrib.staticfiles"],
        STATIC_URL="/static/",
    )
    django.setup()

from pocket.django.storages import ObjectHashStaticStorage  # noqa: E402

REGION = "ap-northeast-1"
BUCKET = "dev-testprj-pocket"
META = ".pocket/static/web/static/"
H1 = "a" * 64
H2 = "b" * 64


def _publish(s3, entries: list[MapEntry]) -> str:
    static_map = StaticMap("web/static")
    for e in entries:
        static_map.add(e)
    s3.put_object(
        Bucket=BUCKET,
        Key=release_key(META, static_map.release_id),
        Body=encode_release(build_release_document(static_map)),
    )
    s3.put_object(
        Bucket=BUCKET,
        Key=current_key(META),
        Body=encode_current(build_current_document(static_map, "2026-01-01T00:00:00")),
    )
    return static_map.release_id


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        yield client


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _resolver(s3, clock) -> StaticResolver:
    return StaticResolver(
        bucket=BUCKET,
        meta_prefix=META,
        base_url="https://cdn.example.com/static/",
        refresh_interval=60,
        s3_client=s3,
        clock=clock,
    )


def test_url_resolves_and_encodes(s3):
    _publish(
        s3,
        [
            MapEntry("img/写真 1.jpg", H1, "data", True),
            MapEntry("app/chunk.js", H2, "code", False),
        ],
    )
    resolver = _resolver(s3, _Clock())
    assert (
        resolver.url("img/写真 1.jpg")
        == "https://cdn.example.com/static/img/%E5%86%99%E7%9C%9F%201.aaaaaaaaaaaaaaaa.jpg"
    )
    assert resolver.url("app/chunk.js") == "https://cdn.example.com/static/app/chunk.js"


def test_unknown_path_raises(s3):
    release = _publish(s3, [MapEntry("a.txt", H1, "data", True)])
    resolver = _resolver(s3, _Clock())
    with pytest.raises(StaticPathNotFound) as exc:
        resolver.url("missing.txt")
    assert exc.value.release_id == release
    assert isinstance(exc.value, KeyError)


def test_initial_load_failure_raises_and_retries(s3):
    resolver = _resolver(s3, _Clock())
    with pytest.raises(Exception):  # noqa: B017 current が無い (NoSuchKey) で落ちる
        resolver.url("a.txt")
    _publish(s3, [MapEntry("a.txt", H1, "data", True)])
    assert resolver.url("a.txt").endswith("/a.aaaaaaaaaaaaaaaa.txt")


def test_refresh_only_after_interval_and_when_etag_changes(s3):
    clock = _Clock()
    _publish(s3, [MapEntry("a.txt", H1, "data", True)])
    resolver = _resolver(s3, clock)
    old = resolver.url("a.txt")
    _publish(s3, [MapEntry("a.txt", H2, "data", True)])
    # 期限前は読み直さない
    clock.now += 30
    assert resolver.url("a.txt") == old
    clock.now += 31
    assert resolver.url("a.txt").endswith("/a.bbbbbbbbbbbbbbbb.txt")


def test_refresh_failure_keeps_previous_table(s3):
    clock = _Clock()
    release = _publish(s3, [MapEntry("a.txt", H1, "data", True)])
    resolver = _resolver(s3, clock)
    old = resolver.url("a.txt")
    # current だけ壊れた release を指す
    s3.put_object(
        Bucket=BUCKET,
        Key=current_key(META),
        Body=json.dumps({"version": 1, "release": "0" * 16}).encode(),
    )
    clock.now += 61
    assert resolver.url("a.txt") == old
    assert resolver.release_id == release


def test_get_static_resolver_from_settings(use_toml, monkeypatch, s3):
    use_toml("tests/data/toml/cloudfront_object_hash.toml")
    monkeypatch.setenv("POCKET_CLOUDFRONT_WEB_DOMAIN", "d123.cloudfront.net")
    monkeypatch.setattr(resolver_module, "_resolvers", {})
    _publish(s3, [MapEntry("a.txt", H1, "data", True)])
    resolver = get_static_resolver("web", "static", stage="dev")
    assert resolver.bucket == BUCKET
    assert resolver.meta_prefix == META
    assert resolver.url("a.txt") == (
        "https://d123.cloudfront.net/static/a.aaaaaaaaaaaaaaaa.txt"
    )
    # ref 省略でも object_hash route が 1 つなら特定できる。同じ instance を返す
    assert get_static_resolver("web", "static", stage="dev") is resolver


def test_get_static_resolver_falls_back_to_declared_domain(use_toml, monkeypatch, s3):
    use_toml("tests/data/toml/cloudfront_object_hash.toml")
    monkeypatch.delenv("POCKET_CLOUDFRONT_WEB_DOMAIN", raising=False)
    monkeypatch.setattr(resolver_module, "_resolvers", {})
    resolver = get_static_resolver("web", stage="dev")
    assert resolver.base_url == "https://cdn.example.com/static/"


def test_get_static_resolver_unknown_route(use_toml, monkeypatch):
    use_toml("tests/data/toml/cloudfront_object_hash.toml")
    monkeypatch.setattr(resolver_module, "_resolvers", {})
    with pytest.raises(RuntimeError, match="特定できません"):
        get_static_resolver("web", "nope", stage="dev")


class _FakeResolver:
    def url(self, path):
        if path == "known.css":
            return "https://cdn.example.com/static/known.x.css"
        raise StaticPathNotFound(path, "r")


def test_django_storage_delegates_to_resolver(monkeypatch):
    monkeypatch.setattr(
        "pocket.static.get_static_resolver", lambda *a, **k: _FakeResolver()
    )
    storage = ObjectHashStaticStorage(distribution="web", route="static")
    assert storage.url("known.css") == "https://cdn.example.com/static/known.x.css"
    assert storage.exists("known.css") is True
    assert storage.exists("nope.css") is False
    with pytest.raises(StaticPathNotFound):
        storage.url("nope.css")
    with pytest.raises(NotImplementedError):
        storage.save("x.css", None)
