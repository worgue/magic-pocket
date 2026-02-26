import boto3
import pytest
from moto import mock_aws

from pocket.context import Context
from pocket.django.settings import DjangoStorage
from pocket.settings import CloudFront, Route, Settings


def test_settings_from_toml(use_toml):
    use_toml("tests/data/toml/default.toml")
    settings = Settings.from_toml(stage="dev")
    assert settings.project_name == "testprj"


@mock_aws
def test_context(use_toml):
    use_toml("tests/data/toml/default.toml")
    res = boto3.client("route53").create_hosted_zone(
        Name="project.com.", CallerReference="test"
    )
    hosted_zone_id = res["HostedZone"]["Id"][len("/hostedzone/") :]
    context = Context.from_toml(stage="dev")
    assert context.project_name == "testprj"
    assert context.awscontainer
    handlers = context.awscontainer.handlers
    assert handlers["wsgi"].apigateway
    assert handlers["wsgi"].apigateway.hosted_zone_id == hosted_zone_id
    assert handlers["sqsmanagement"].sqs
    assert handlers["sqsmanagement"].sqs.name == "dev-testprj-pocket-sqsmanagement"
    # CloudFront は S3 バケットを共有
    assert context.cloudfront
    assert "main" in context.cloudfront
    assert context.s3
    assert context.cloudfront["main"].bucket_name == context.s3.bucket_name
    assert context.cloudfront["main"].default_route.origin_path == "/main"


@mock_aws
def test_signing_key_imports(use_toml):
    use_toml("tests/data/toml/cloudfront_signing_key.toml")
    context = Context.from_toml(stage="dev")
    assert context.awscontainer
    # signing_key_imports に CF_MEDIA_KEY_ID → Export名 のマッピングがある
    imports = context.awscontainer.signing_key_imports
    assert "CF_MEDIA_KEY_ID" in imports
    assert imports["CF_MEDIA_KEY_ID"] == "dev-testprj-media-public-key-id"


@mock_aws
def test_api_route_context(use_toml):
    use_toml("tests/data/toml/cloudfront_api_route.toml")
    context = Context.from_toml(stage="dev")
    assert context.cloudfront
    cf = context.cloudfront["main"]
    # api_origins が正しく構築されること
    assert "wsgi" in cf.api_origins
    assert cf.api_origins["wsgi"] == "dev-testprj-wsgi-api-domain"
    # api_routes が正しく取得できること
    assert len(cf.api_routes) == 1
    assert cf.api_routes[0].is_api
    assert cf.api_routes[0].handler == "wsgi"
    assert cf.api_routes[0].path_pattern == "/api/*"
    # extra_routes に api route が含まれないこと
    assert all(not r.is_api for r in cf.extra_routes)


@mock_aws
def test_api_route_handler_export(use_toml):
    use_toml("tests/data/toml/cloudfront_api_route.toml")
    context = Context.from_toml(stage="dev")
    assert context.awscontainer
    handler = context.awscontainer.handlers["wsgi"]
    # LambdaHandlerContext.export_api_domain が設定されること
    assert handler.export_api_domain == "dev-testprj-wsgi-api-domain"


@mock_aws
def test_yaml(use_toml):
    use_toml("tests/data/toml/default.toml")
    res = boto3.client("route53").create_hosted_zone(
        Name="project.com.", CallerReference="test"
    )
    hosted_zone_id = res["HostedZone"]["Id"][len("/hostedzone/") :]
    context = Context.from_toml(stage="dev")
    assert context.awscontainer
    handlers = context.awscontainer.handlers
    assert handlers["wsgi"].apigateway
    assert handlers["wsgi"].apigateway.hosted_zone_id == hosted_zone_id


@mock_aws
def test_route_build_dir(use_toml):
    use_toml("tests/data/toml/cloudfront_spa_build.toml")
    context = Context.from_toml(stage="dev")
    cf = context.cloudfront["web"]
    default_route = cf.default_route
    assert default_route.build == "just frontend-build"
    assert default_route.build_dir == "frontend/dist"
    assert default_route.origin_path == "/web/app"


def test_route_build_without_build_dir_fails():
    with pytest.raises(ValueError, match="build_dir is required when build is set"):
        CloudFront.model_validate(
            {
                "routes": [
                    {
                        "is_default": True,
                        "is_spa": True,
                        "build": "npm run build",
                        "origin_path": "/spa",
                    },
                ],
            }
        )


def test_api_route_with_build_fails():
    with pytest.raises(ValueError, match="type = 'api' cannot use build or build_dir"):
        Route.model_validate(
            {
                "type": "api",
                "handler": "wsgi",
                "path_pattern": "/api/*",
                "build_dir": "dist",
            }
        )


@mock_aws
def test_uploadable_routes(use_toml):
    use_toml("tests/data/toml/cloudfront_spa_build.toml")
    context = Context.from_toml(stage="dev")
    cf = context.cloudfront["web"]
    assert len(cf.uploadable_routes) == 1
    assert cf.uploadable_routes[0].build_dir == "frontend/dist"


def test_route_origin_path():
    """S3 route に origin_path 必須、API route に禁止"""
    # S3 route: origin_path 必須
    with pytest.raises(ValueError, match="origin_path is required for S3 routes"):
        Route.model_validate({"is_default": True, "is_spa": True})
    # API route: origin_path 指定禁止
    with pytest.raises(ValueError, match="type = 'api' cannot use origin_path"):
        Route.model_validate(
            {
                "type": "api",
                "handler": "wsgi",
                "path_pattern": "/api/*",
                "origin_path": "/api",
            }
        )
    # origin_path のフォーマット
    with pytest.raises(ValueError, match="origin_path must starts with /"):
        Route.model_validate(
            {"is_default": True, "is_spa": True, "origin_path": "noslash"}
        )
    with pytest.raises(ValueError, match="origin_path must not ends with /"):
        Route.model_validate(
            {"is_default": True, "is_spa": True, "origin_path": "/trailing/"}
        )


@mock_aws
def test_route_build_dir_origin_path(use_toml):
    """build_dir route の origin_path が正しく設定される"""
    use_toml("tests/data/toml/cloudfront_spa_build.toml")
    context = Context.from_toml(stage="dev")
    cf = context.cloudfront["web"]
    default_route = cf.default_route
    assert default_route.origin_path == "/web/app"
    static_route = [r for r in cf.routes if r.path_pattern == "/static/*"][0]
    assert static_route.origin_path == "/web"


def test_storage_location_forbidden_with_distribution():
    """distribution 使用時に location は指定不可"""
    with pytest.raises(ValueError, match="location cannot be used with distribution"):
        DjangoStorage.model_validate(
            {"store": "s3", "location": "static", "distribution": "web"}
        )


def test_storage_route_requires_distribution():
    """route は distribution なしでは使用不可"""
    with pytest.raises(ValueError, match="route requires distribution"):
        DjangoStorage.model_validate(
            {"store": "s3", "location": "media", "route": "static"}
        )
