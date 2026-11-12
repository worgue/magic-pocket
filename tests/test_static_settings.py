"""object_hash の設定検証・context・Django storage の選択・CloudFront template。"""

from __future__ import annotations

import yaml as yaml_lib
from pocket_cli.resources.aws.cloudformation import CloudFrontStack
from pytest import raises

from pocket import settings
from pocket.context import CloudFrontContext, Context, RouteContext
from pocket.django.context import DjangoStorageContext
from pocket.django.settings import DjangoStorage
from pocket.django.utils import get_storages


def _route(**overrides) -> settings.Route:
    data = {
        "path_pattern": "/static/*",
        "ref": "static",
        "versioning": "object_hash",
        "sources": [{"name": "data", "dir": "data"}],
    }
    data.update(overrides)
    return settings.Route.model_validate(data)


def test_object_hash_requires_sources():
    with raises(ValueError, match="sources"):
        _route(sources=[])


def test_sources_and_map_only_with_object_hash():
    with raises(ValueError, match="sources"):
        _route(versioning="content_hash")
    with raises(ValueError, match="map"):
        _route(versioning=None, sources=[], map="x.map")


def test_object_hash_excludes_build_upload_dir_signed_spa():
    with raises(ValueError, match="upload_dir"):
        _route(upload_dir="dist")
    with raises(ValueError, match="build"):
        _route(build={"dir": "dist", "cmd": "make"})
    with raises(ValueError, match="signed"):
        _route(signed=True)
    with raises(ValueError, match="is_spa"):
        _route(is_spa=True)
    with raises(ValueError, match="delete_stale"):
        _route(delete_stale=True)


def test_duplicate_source_names_rejected():
    with raises(ValueError, match="重複"):
        _route(sources=[{"name": "a", "dir": "x"}, {"name": "a", "dir": "y"}])


def test_static_source_field_rules():
    with raises(ValueError, match="name"):
        settings.StaticSource(name="bad name", dir="x")
    with raises(ValueError, match="相対"):
        settings.StaticSource(name="a", dir="/abs")
    with raises(ValueError, match="先頭"):
        settings.StaticSource(name="a", dir="x", prefix="/img/")
    with raises(ValueError, match="末尾"):
        settings.StaticSource(name="a", dir="x", prefix="img")
    ok = settings.StaticSource(name="a", dir="x", prefix="img/", hashed=False)
    assert ok.prefix == "img/" and ok.hashed is False


def test_pocket_prefix_path_pattern_rejected():
    with raises(ValueError, match=".pocket"):
        settings.Route.model_validate({"path_pattern": "/.pocket/*"})


def test_route_context_derivations():
    ctx = RouteContext.from_settings(_route(), cloudfront_name="web")
    assert ctx.is_object_hash
    assert ctx.key_prefix == "static/"
    assert ctx.url_prefix == "/static/"
    assert ctx.static_meta_prefix == ".pocket/static/web/static/"
    assert ctx.map_path == "pocket_static/web-static.map"
    assert ctx.delete_stale is False
    assert [s.name for s in ctx.sources] == ["data"]

    with_origin = RouteContext.from_settings(
        _route(origin_path="/assets", map="maps/static.map"), cloudfront_name="web"
    )
    assert with_origin.key_prefix == "assets/static/"
    assert with_origin.url_prefix == "/static/"
    assert with_origin.map_path == "maps/static.map"


def test_context_from_toml(use_toml):
    use_toml("tests/data/toml/cloudfront_object_hash.toml")
    context = Context.from_toml(stage="dev")
    cf = context.cloudfront["web"]
    route = cf.get_route("static")
    assert route.is_object_hash
    assert route.cloudfront_name == "web"
    assert route.sources[1].prefix == "app/"
    assert route.sources[1].hashed is False
    assert cf.bucket_policy_prefix == "/static"


def test_django_storage_backend_is_object_hash(use_toml):
    use_toml("tests/data/toml/cloudfront_object_hash.toml")
    storages = get_storages(stage="dev")
    static = storages["staticfiles"]
    assert static["BACKEND"] == "pocket.django.storages.ObjectHashStaticStorage"
    assert static["OPTIONS"] == {"distribution": "web", "route": "static"}


def test_django_storage_object_hash_requires_static():
    cf = settings.CloudFront.model_validate(
        {
            "routes": [
                {"type": "lambda", "handler": "main.wsgi", "is_default": True},
                _route().model_dump(),
            ]
        }
    )
    storage = DjangoStorage(store="s3", distribution="web", route="static")
    with raises(ValueError, match="static = true"):
        DjangoStorageContext.from_settings(
            storage, cloudfront_distributions={"web": cf}
        )


def _stack(routes):
    ctx = CloudFrontContext(
        name="web",
        region="ap-northeast-1",
        s3_region="ap-northeast-1",
        stage="dev",
        domain="cdn.example.com",
        hosted_zone_id_override="ZPARENT",
        slug="dev-testprj-web",
        bucket_name="dev-testprj-bucket",
        resource_prefix="dev-testprj-",
        routes=routes,
    )
    stack = CloudFrontStack(ctx)
    stack._resolve_acm_arn = lambda: "arn:aws:acm:us-east-1:0:certificate/x"
    stack._resolve_waf_arn = lambda: None
    return stack


def test_cloudfront_template_object_hash_route():
    """cache-control の Function は付き、deploy_hash の strip Function は付かない。"""
    routes = [
        RouteContext(is_default=True, origin_path="/spa", is_spa=True),
        RouteContext.from_settings(_route(), cloudfront_name="web"),
    ]
    stack = _stack(routes)
    resources = yaml_lib.safe_load(stack.yaml)["Resources"]
    assert "CacheControlFunctionStatic" in resources
    assert "DeployHashStripFunctionStatic" not in resources
    assert "DeployHashCachePolicyStatic" not in resources
    behaviors = resources["CloudFrontDistribution"]["Properties"]["DistributionConfig"][
        "CacheBehaviors"
    ]
    static = [b for b in behaviors if b["PathPattern"] == "/static/*"][0]
    assert static["CachePolicyId"] == "658327ea-f89d-4fab-a63d-7e88639e58f6"
    assert [a["EventType"] for a in static["FunctionAssociations"]] == [
        "viewer-response"
    ]
