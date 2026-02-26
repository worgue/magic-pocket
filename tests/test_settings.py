import boto3
import pytest
from moto import mock_aws

from pocket.context import Context
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
    assert context.cloudfront["main"].origin_prefix == "/spa"


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
    cf = context.cloudfront["main"]
    default_route = cf.default_route
    assert default_route.build == "just frontend-build"
    assert default_route.build_dir == "frontend/dist"


def test_route_build_without_build_dir_fails():
    with pytest.raises(ValueError, match="build_dir is required when build is set"):
        CloudFront.model_validate(
            {
                "origin_prefix": "/spa",
                "routes": [
                    {"is_default": True, "is_spa": True, "build": "npm run build"},
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
    cf = context.cloudfront["main"]
    assert len(cf.uploadable_routes) == 1
    assert cf.uploadable_routes[0].build_dir == "frontend/dist"
