import os
from unittest.mock import patch

import pytest
from moto import mock_aws

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
    assert context.awscontainer
    assert context.awscontainer.envs.get("DEPLOY_HASH") == "abc1234"


@mock_aws
def test_deploy_hash_cf_function_rendering(use_toml):
    """deploy_hash route 用の CF Function が生成されること"""
    with patch.dict(os.environ, {"DEPLOY_HASH": "abc1234"}):
        use_toml("tests/data/toml/cloudfront_deploy_hash.toml")
        context = Context.from_toml(stage="dev")
    from pocket_cli.resources.aws.cloudformation import CloudFrontStack

    cf = context.cloudfront["web"]
    stack = CloudFrontStack(cf)
    stack._resolve_acm_arns = lambda: (None, {})
    yaml = stack.yaml
    assert "DeployHashStripFunctionStatic" in yaml
    assert "deploy-hash-strip" in yaml
    assert "abc1234" in yaml
    assert 'request.uri.replace("/abc1234/", "/")' in yaml
    # ResponseHeadersPolicy も存在する
    assert "ResponseHeadersPolicyStatic" in yaml
    # versioned ルートの cache-control は public / immutable 付き
    assert 'Value: "public, max-age=31536000, immutable"' in yaml


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
    from pocket_cli.resources.aws.cloudformation import CloudFrontStack

    cf = context.cloudfront["web"]
    stack = CloudFrontStack(cf)
    yaml = stack.yaml
    assert "DeployHashStripFunction" not in yaml
    # content_hash の ResponseHeadersPolicy はある
    assert "ResponseHeadersPolicy" in yaml
