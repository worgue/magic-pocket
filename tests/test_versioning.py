import os
from unittest.mock import patch

import pytest
import yaml as yaml_lib
from moto import mock_aws
from pocket_cli.resources.aws.cloudformation import CloudFrontStack

from pocket.context import Context
from pocket.settings import CloudFront, Route


def test_legacy_is_versioned_rejected():
    """旧 is_versioned は明示エラーになること"""
    with pytest.raises(ValueError, match="is_versioned は廃止"):
        Route.model_validate(
            {
                "is_default": True,
                "is_spa": False,
                "is_versioned": True,
                "origin_path": "/static",
            }
        )


def test_versioning_content_hash():
    cf = CloudFront.model_validate(
        {
            "routes": [
                {"is_default": True, "is_spa": True, "origin_path": "/app"},
                {
                    "path_pattern": "/static/*",
                    "versioning": "content_hash",
                    "origin_path": "/static",
                },
            ],
        }
    )
    static_route = cf.routes[1]
    assert static_route.versioning == "content_hash"


def test_versioning_deploy_hash():
    cf = CloudFront.model_validate(
        {
            "routes": [
                {"is_default": True, "is_spa": True, "origin_path": "/app"},
                {
                    "path_pattern": "/static/*",
                    "versioning": "deploy_hash",
                    "origin_path": "/static",
                },
            ],
        }
    )
    static_route = cf.routes[1]
    assert static_route.versioning == "deploy_hash"


def test_is_spa_and_versioning_exclusive():
    with pytest.raises(ValueError, match="is_spa と versioning"):
        Route.model_validate(
            {
                "is_default": True,
                "is_spa": True,
                "versioning": "content_hash",
                "origin_path": "/app",
            }
        )


@mock_aws
def test_deploy_hash_context(use_toml):
    """deploy_hash route が context に正しく反映されること"""
    with patch.dict(os.environ, {"DEPLOY_HASH": "abc1234"}):
        use_toml("tests/data/toml/cloudfront_deploy_hash.toml")
        context = Context.from_toml(stage="dev")
    assert context.cloudfront
    cf = context.cloudfront["web"]
    assert cf.deploy_hash == "abc1234"
    static_route = [r for r in cf.routes if r.path_pattern == "/static/*"][0]
    assert static_route.is_deploy_hash
    assert not static_route.is_content_hash
    # DEPLOY_HASH が awscontainer envs に注入される
    assert context.container["main"]
    assert context.container["main"].envs.get("DEPLOY_HASH") == "abc1234"


@mock_aws
def test_deploy_hash_injected_without_deploy_hash_route(use_toml):
    """deploy_hash route が無い構成でも DEPLOY_HASH が全 container の envs に注入
    されること (KN1450: route の撤去で他 container の版識別が消えない)"""
    with patch.dict(os.environ, {"DEPLOY_HASH": "abc1234"}):
        use_toml("tests/data/toml/default.toml")
        context = Context.from_toml(stage="dev")
    assert not any(cf.deploy_hash for cf in context.cloudfront.values())
    assert context.container["main"].envs.get("DEPLOY_HASH") == "abc1234"


def test_deploy_hash_cf_function_rendering(use_toml):
    """deploy_hash route 用の CF Function が生成されること"""
    with patch.dict(os.environ, {"DEPLOY_HASH": "abc1234"}):
        use_toml("tests/data/toml/cloudfront_deploy_hash.toml")
        context = Context.from_toml(stage="dev")
    cf = context.cloudfront["web"]
    stack = CloudFrontStack(cf)
    stack._resolve_acm_arn = lambda: None
    yaml = stack.yaml
    assert "DeployHashStripFunctionStatic" in yaml
    assert "deploy-hash-strip" in yaml
    # git hash の形なら値を問わず外すので、現在の hash は Function に埋め込まない
    # (deploy のたびに Function が変わらない = 切り替えの窓が無い。KN1670)
    assert "abc1234" not in yaml
    assert "var prefix = '/static/';" in yaml
    assert "if (/^[0-9a-f]{7,40}$/.test(segment)) {" in yaml
    # cache-control は viewer-response Function が成功応答だけに付ける
    assert "AWS::CloudFront::ResponseHeadersPolicy" not in yaml
    assert "CacheControlFunctionStatic" in yaml
    assert "'public, max-age=31536000, immutable'" in yaml
    assert "(status >= 200 && status < 300) || status === 304" in yaml
    behavior = _static_behavior(yaml)
    assert [a["EventType"] for a in behavior["FunctionAssociations"]] == [
        "viewer-request",
        "viewer-response",
    ]
    assert "ResponseHeadersPolicyId" not in behavior
    # 外した hash は header に載せ、CachePolicy でキャッシュキーに含める
    # (URI は hash を外した後の値がキーになるため)
    assert "request.headers['x-pocket-deploy-hash'] = { value: segment };" in yaml
    assert "delete request.headers['x-pocket-deploy-hash'];" in yaml
    assert behavior["CachePolicyId"] == {"Ref": "DeployHashCachePolicyStatic"}
    policy = yaml_lib.safe_load(yaml)["Resources"]["DeployHashCachePolicyStatic"]
    key_params = policy["Properties"]["CachePolicyConfig"][
        "ParametersInCacheKeyAndForwardedToOrigin"
    ]
    assert key_params["HeadersConfig"] == {
        "HeaderBehavior": "whitelist",
        "Headers": ["x-pocket-deploy-hash"],
    }


def test_deploy_hash_cf_function_keeps_custom_hash(use_toml):
    """DEPLOY_HASH を git hash の形でない値で上書きした場合は、その値も外すこと"""
    with patch.dict(os.environ, {"DEPLOY_HASH": "v1.2.3"}):
        use_toml("tests/data/toml/cloudfront_deploy_hash.toml")
        context = Context.from_toml(stage="dev")
    stack = CloudFrontStack(context.cloudfront["web"])
    stack._resolve_acm_arn = lambda: None
    assert (
        'if (/^[0-9a-f]{7,40}$/.test(segment) || segment === "v1.2.3") {' in stack.yaml
    )


def test_deploy_hash_storage_backend():
    """deploy_hash route の storage は StaticFilesStorage を返すこと"""
    from pocket.django.context import DjangoStorageContext

    ctx = DjangoStorageContext(
        store="s3",
        static=True,
        distribution="web",
        route="static",
        deploy_hash=True,
    )
    assert ctx.backend == "django.contrib.staticfiles.storage.StaticFilesStorage"


def test_content_hash_storage_backend():
    """deploy_hash なしの S3 static storage は CloudFrontS3StaticStorage"""
    from pocket.django.context import DjangoStorageContext

    ctx = DjangoStorageContext(
        store="s3",
        static=True,
        distribution="web",
        route="static",
        deploy_hash=False,
    )
    assert ctx.backend == "pocket.django.storages.CloudFrontS3StaticStorage"


@mock_aws
def test_content_hash_no_deploy_hash_function(use_toml):
    """content_hash route では DeployHashStripFunction は生成されないこと"""
    use_toml("tests/data/toml/cloudfront_spa_build.toml")
    context = Context.from_toml(stage="dev")
    cf = context.cloudfront["web"]
    stack = CloudFrontStack(cf)
    yaml = stack.yaml
    assert "DeployHashStripFunction" not in yaml
    assert "DeployHashCachePolicy" not in yaml
    # content_hash も cache-control は viewer-response Function で付ける
    assert "AWS::CloudFront::ResponseHeadersPolicy" not in yaml
    assert "CacheControlFunction" in yaml


def _static_behavior(yaml: str) -> dict:
    template = yaml_lib.safe_load(yaml)
    config = template["Resources"]["CloudFrontDistribution"]["Properties"][
        "DistributionConfig"
    ]
    return next(b for b in config["CacheBehaviors"] if b["PathPattern"] == "/static/*")
