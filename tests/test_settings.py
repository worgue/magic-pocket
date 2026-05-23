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
def test_lambda_route_context(use_toml):
    use_toml("tests/data/toml/cloudfront_api_route.toml")
    context = Context.from_toml(stage="dev")
    assert context.cloudfront
    cf = context.cloudfront["main"]
    # api_origins が正しく構築されること
    assert "wsgi" in cf.api_origins
    assert cf.api_origins["wsgi"] == "dev-testprj-wsgi-api-domain"
    # extra_lambda_routes が正しく取得できること
    assert len(cf.extra_lambda_routes) == 1
    assert cf.extra_lambda_routes[0].is_lambda
    assert cf.extra_lambda_routes[0].handler == "wsgi"
    assert cf.extra_lambda_routes[0].path_pattern == "/api/*"
    # extra_s3_routes に lambda route が含まれないこと
    assert all(not r.is_lambda for r in cf.extra_s3_routes)


@mock_aws
def test_lambda_default_route_context(use_toml):
    """Django 単体構成（is_default = true で type = lambda）が設定可能なこと"""
    use_toml("tests/data/toml/cloudfront_api_default.toml")
    context = Context.from_toml(stage="dev")
    assert context.cloudfront
    cf = context.cloudfront["main"]
    # default_route が lambda route になること
    assert cf.default_route.is_lambda
    assert cf.default_route.handler == "wsgi"
    assert cf.default_route.is_default
    # api_origins には default route 由来のエントリも入る
    assert "wsgi" in cf.api_origins
    # extra_lambda_routes は default route を除外する（CacheBehaviors への重複防止）
    assert cf.extra_lambda_routes == []
    # has_lambda_route は default route を含めて True
    assert cf.has_lambda_route


def test_legacy_api_type_rejected():
    """旧 type = "api" は明示的にエラーになること"""
    with pytest.raises(ValueError, match='type = "api" は廃止'):
        Route.model_validate(
            {
                "type": "api",
                "handler": "wsgi",
                "path_pattern": "/api/*",
            }
        )


@mock_aws
def test_lambda_route_handler_export(use_toml):
    use_toml("tests/data/toml/cloudfront_api_route.toml")
    context = Context.from_toml(stage="dev")
    assert context.awscontainer
    handler = context.awscontainer.handlers["wsgi"]
    # LambdaHandlerContext.export_api_domain が設定されること
    assert handler.export_api_domain == "dev-testprj-wsgi-api-domain"


@mock_aws
def test_cloudfront_yaml_works_without_awscontainer_deployed(use_toml):
    """lambda route ありの構成で、awscontainer 未 deploy でも CloudFront yaml が
    render できること (Fn::ImportValue 化の回帰テスト)。

    過去は `_resolve_api_origins` が boto3.list_exports で実値を埋めにいくため、
    awscontainer stack が未 deploy だと RuntimeError で fail していた。
    現在は ImportValue で template に書き出すだけなので AWS API 不要、
    dry-run が機能する。
    """
    from pocket_cli.resources.aws.cloudformation import CloudFrontStack

    use_toml("tests/data/toml/cloudfront_api_route.toml")
    context = Context.from_toml(stage="dev")
    cf = context.cloudfront["main"]
    yaml = CloudFrontStack(cf).yaml
    # API origin は Fn::ImportValue で参照されている (literal domain ではない)
    assert "Fn::ImportValue" in yaml
    assert "dev-testprj-wsgi-api-domain" in yaml
    # boto3.list_exports は呼ばれていない (= AWS account 不要で render 成功)
    # ImportValue 名が literal で template に出ること、で間接的に確認


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


def test_lambda_route_with_build_fails():
    with pytest.raises(
        ValueError, match="type = 'lambda' cannot use build or build_dir"
    ):
        Route.model_validate(
            {
                "type": "lambda",
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
    # S3 route: origin_path 必須。エラー文に具体例 (/" や /spa) と Example 行を含めて
    # 利用者が次の一手を取れるようにする
    with pytest.raises(ValueError) as excinfo:
        Route.model_validate({"is_default": True, "is_spa": True})
    msg = str(excinfo.value)
    assert "S3 route requires `origin_path`" in msg
    assert '"/"' in msg  # bucket root の例示
    assert "/spa" in msg  # prefix 切り出しの例示
    assert "Example:" in msg
    # Lambda route: origin_path 指定禁止
    with pytest.raises(ValueError, match="type = 'lambda' cannot use origin_path"):
        Route.model_validate(
            {
                "type": "lambda",
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


def test_route_ref_duplicate_rejected():
    """同じ ref を複数 route に設定するとエラーになること"""
    with pytest.raises(ValueError, match="ref 'static' が重複"):
        CloudFront.model_validate(
            {
                "routes": [
                    {
                        "is_default": True,
                        "is_spa": True,
                        "origin_path": "/web",
                    },
                    {
                        "path_pattern": "/static/*",
                        "ref": "static",
                        "versioning": "content_hash",
                        "origin_path": "/static",
                    },
                    {
                        "path_pattern": "/app/*",
                        "ref": "static",
                        "origin_path": "/app",
                    },
                ],
            }
        )


def test_route_s3_prefix_overlap_parent_child():
    """SPA ルートと static ルートの S3 prefix が親子関係の場合エラー"""
    # 今回のインシデント: staticfiles に route 未指定で
    # SPA デフォルトルート (origin_path="/web/app") にフォールバックした場合、
    # deploystatic の --delete が SPA ファイルを削除してしまう。
    # この設定では /web/app が /web/app/static の親になるため検出される。
    with pytest.raises(ValueError, match="子パス"):
        CloudFront.model_validate(
            {
                "routes": [
                    {
                        "is_default": True,
                        "is_spa": True,
                        "origin_path": "/web/app",
                    },
                    {
                        "path_pattern": "/static/*",
                        "versioning": "content_hash",
                        "origin_path": "/web/app",
                    },
                ],
            }
        )


def test_route_s3_prefix_overlap_same():
    """S3 prefix が完全に同一の場合エラー"""
    with pytest.raises(ValueError, match="同一"):
        CloudFront.model_validate(
            {
                "routes": [
                    {
                        "is_default": True,
                        "is_spa": True,
                        "origin_path": "/web/app",
                    },
                    {
                        "path_pattern": "/app/*",
                        "versioning": "content_hash",
                        "origin_path": "/web",
                    },
                ],
            }
        )


def test_route_s3_prefix_no_overlap():
    """S3 prefix が分離されている場合は正常"""
    # 今回の修正後の正しい設定: SPA=/web/app, static=/web/static
    cf = CloudFront.model_validate(
        {
            "routes": [
                {
                    "is_default": True,
                    "is_spa": True,
                    "origin_path": "/web/app",
                },
                {
                    "path_pattern": "/static/*",
                    "ref": "static",
                    "versioning": "content_hash",
                    "origin_path": "/web",
                },
            ],
        }
    )
    assert len(cf.routes) == 2


def test_route_s3_prefix_overlap_with_lambda_ignored():
    """Lambda ルートは S3 を使わないので重複チェック対象外"""
    cf = CloudFront.model_validate(
        {
            "routes": [
                {
                    "is_default": True,
                    "is_spa": True,
                    "origin_path": "/web/app",
                },
                {
                    "path_pattern": "/api/*",
                    "type": "lambda",
                    "handler": "wsgi",
                },
                {
                    "path_pattern": "/static/*",
                    "ref": "static",
                    "versioning": "content_hash",
                    "origin_path": "/web",
                },
            ],
        }
    )
    assert len(cf.routes) == 3
