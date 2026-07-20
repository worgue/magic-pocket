"""cloudfront.basic_auth (distribution 全体の Basic 認証) のテスト。

- settings: basic_auth 参照と basic_auth_credential options の fail-loud 検証
- mediator: "user:pass" credential の生成 (固定 password / ランダム)
- テンプレート: 全 behavior への prelude 注入 / 単体 BasicAuthFunction / KVS
- resource: 期待 Authorization ヘッダ値の組み立てと KVS 書き込み
"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest
import yaml as yaml_lib
from pocket_cli.mediator import Mediator
from pocket_cli.resources.aws.cloudformation import CloudFrontStack

from pocket.context import CloudFrontContext, RedirectFromContext, RouteContext
from pocket.settings import ManagedSecretSpec, Settings

# --- settings バリデーション ---


def _settings_dict(cf_extra: dict, secrets_managed: dict | None):
    data: dict = {
        "stage": "dev",
        "general": {
            "region": "ap-southeast-1",
            "project_name": "testprj",
            "stages": ["dev"],
        },
        "s3": {},
        "cloudfront": {
            "web": {
                "routes": [{"is_default": True, "origin_path": "/main"}],
                **cf_extra,
            }
        },
    }
    if secrets_managed is not None:
        data["awscontainer"] = {
            "dockerfile_path": "Dockerfile",
            "handlers": {
                "wsgi": {"command": "pocket.django.lambda_handlers.wsgi_handler"}
            },
            "secrets": {"managed": secrets_managed},
        }
    return data


_BA_SPEC = {"type": "basic_auth_credential", "options": {"username": "u"}}


def test_basic_auth_requires_awscontainer_secrets():
    with pytest.raises(ValueError, match="awscontainer.secrets is required"):
        Settings.model_validate(_settings_dict({"basic_auth": "BA"}, None))


def test_basic_auth_key_must_exist_in_managed():
    with pytest.raises(ValueError, match="not found in awscontainer.secrets.managed"):
        Settings.model_validate(
            _settings_dict({"basic_auth": "MISSING"}, {"BA": _BA_SPEC})
        )


def test_basic_auth_key_must_be_credential_type():
    """password 等の他 type を参照すると 'user:pass' 形式にならず silent に壊れる"""
    with pytest.raises(ValueError, match="basic_auth_credential"):
        Settings.model_validate(
            _settings_dict({"basic_auth": "BA"}, {"BA": {"type": "password"}})
        )


def test_basic_auth_valid_reference_ok():
    settings = Settings.model_validate(
        _settings_dict({"basic_auth": "BA"}, {"BA": _BA_SPEC})
    )
    assert settings.cloudfront["web"].basic_auth == "BA"


def test_credential_spec_requires_username():
    with pytest.raises(ValueError, match="options.username"):
        ManagedSecretSpec.model_validate({"type": "basic_auth_credential"})


def test_credential_spec_rejects_colon_in_username():
    with pytest.raises(ValueError, match="':'"):
        ManagedSecretSpec.model_validate(
            {"type": "basic_auth_credential", "options": {"username": "a:b"}}
        )


def test_credential_spec_rejects_non_str_password():
    with pytest.raises(ValueError, match="password"):
        ManagedSecretSpec.model_validate(
            {
                "type": "basic_auth_credential",
                "options": {"username": "u", "password": 5},
            }
        )


# --- mediator 生成 ---


def _generate(spec_dict):
    mediator = Mediator(MagicMock())
    return mediator._generate_secret(ManagedSecretSpec.model_validate(spec_dict))


def test_mediator_generates_fixed_password_credential():
    value = _generate(
        {
            "type": "basic_auth_credential",
            "options": {"username": "monban-user", "password": "fixed-pass"},
        }
    )
    assert value == "monban-user:fixed-pass"


def test_mediator_generates_random_password_credential():
    value = _generate(
        {"type": "basic_auth_credential", "options": {"username": "u", "length": 20}}
    )
    assert isinstance(value, str)
    username, password = value.split(":", 1)
    assert username == "u"
    assert len(password) == 20
    assert ":" not in password


# --- テンプレートレンダリング ---


def _stack(*, routes, basic_auth: str | None = "BA", redirect_from=None, **overrides):
    ctx = CloudFrontContext(
        name="web",
        region="ap-northeast-1",
        s3_region="ap-northeast-1",
        stage="dev",
        domain=overrides.pop("domain", "www.example.com"),
        hosted_zone_id_override="ZPARENT",
        slug="dev-testprj-web",
        bucket_name="dev-testprj-bucket",
        resource_prefix="dev-testprj-",
        redirect_from=redirect_from or [],
        routes=routes,
        basic_auth=basic_auth,
        **overrides,
    )
    stack = CloudFrontStack(ctx)
    stack._resolve_acm_arn = lambda: "arn:aws:acm:us-east-1:0:certificate/x"
    stack._resolve_waf_arn = lambda: None
    return stack


def _doc(stack):
    return yaml_lib.safe_load(stack.yaml)


def test_kvs_created_without_require_token_routes():
    """basic_auth だけでも TokenKvs (共用 KVS) が作られること"""
    res = _doc(_stack(routes=[RouteContext(is_default=True)]))["Resources"]
    assert "TokenKvs" in res
    assert (
        "TokenKvsArn" in _doc(_stack(routes=[RouteContext(is_default=True)]))["Outputs"]
    )


def test_plain_s3_default_gets_basic_auth_function():
    res = _doc(_stack(routes=[RouteContext(is_default=True)]))["Resources"]
    assert "BasicAuthFunction" in res
    assert "HostRedirectFunction" not in res
    dcb = res["CloudFrontDistribution"]["Properties"]["DistributionConfig"][
        "DefaultCacheBehavior"
    ]
    assert "BasicAuthFunction" in yaml_lib.dump(dcb["FunctionAssociations"])
    fn = res["BasicAuthFunction"]["Properties"]
    code = fn["FunctionCode"]
    assert "async function handler" in code
    assert "cf.kvs()" in code
    assert "401" in code
    assert "www-authenticate" in code
    assert fn["FunctionConfig"]["KeyValueStoreAssociations"][0]["KeyValueStoreARN"] == {
        "Fn::GetAtt": "TokenKvs.Arn"
    }


def test_spa_fallback_function_gets_prelude_and_kvs():
    res = _doc(_stack(routes=[RouteContext(is_default=True, is_spa=True)]))["Resources"]
    fn = res["UrlFallbackFunctionRoot"]["Properties"]
    code = fn["FunctionCode"]
    assert "async function handler" in code
    assert "cf.kvs()" in code
    assert "401" in code
    assert "fallback" in code or "request.uri" in code  # 元のロジックが残る
    assert "KeyValueStoreAssociations" in fn["FunctionConfig"]
    # 単体 BasicAuthFunction はあるが、SPA behavior は自分の Function を使う
    dcb = res["CloudFrontDistribution"]["Properties"]["DistributionConfig"][
        "DefaultCacheBehavior"
    ]
    assert "UrlFallbackFunction" in yaml_lib.dump(dcb["FunctionAssociations"])


def test_lambda_route_gets_prelude_via_api_host_function():
    """lambda behavior は共有 ApiHostFunction に prelude が入り KVS が付く"""
    res = _doc(
        _stack(
            routes=[RouteContext(is_default=True, type="lambda", handler="api")],
            api_origins={"api": "ExportApiOrigin"},
        )
    )["Resources"]
    fn = res["ApiHostFunction"]["Properties"]
    code = fn["FunctionCode"]
    assert "async function handler" in code
    assert "401" in code
    assert "x-forwarded-host" in code  # 元のロジックが残る
    assert "KeyValueStoreAssociations" in fn["FunctionConfig"]


def test_redirect_from_prelude_merged_into_basic_auth_function():
    """redirect_from 併用時は単体 Function が BasicAuthFunction に一本化され、
    redirect prelude が basic auth 判定より先に実行されること"""
    res = _doc(
        _stack(
            routes=[RouteContext(is_default=True)],
            redirect_from=[
                RedirectFromContext(
                    domain="old.example.com", hosted_zone_id_override="ZRF"
                )
            ],
        )
    )["Resources"]
    assert "HostRedirectFunction" not in res
    code = res["BasicAuthFunction"]["Properties"]["FunctionCode"]
    assert "https://www.example.com" in code  # redirect prelude
    assert "401" in code
    assert code.index("301") < code.index("401")  # redirect が先


def test_spa_auth_function_gets_basic_auth_prelude():
    """require_token 併用時、spa auth Function に basic auth prelude が同居する"""
    res = _doc(
        _stack(
            routes=[
                RouteContext(
                    is_default=True, is_spa=True, require_token=True, login_path="/l"
                )
            ],
            token_secret="TOKEN",  # noqa: S106 (secret キー名であって値ではない)
        )
    )["Resources"]
    fc = res["UrlFallbackFunctionRoot"]["Properties"]["FunctionCode"]
    body = fc["Fn::Sub"][0]
    assert "401" in body
    assert "token_secret" in body  # 元の spa auth ロジックが残る
    assert body.count("kvsHandle =") == 1  # handle 宣言が重複しない


def test_deploy_hash_function_gets_prelude_and_kvs():
    res = _doc(
        _stack(
            routes=[
                RouteContext(is_default=True),
                RouteContext(path_pattern="/v", versioning="deploy_hash"),
            ],
            deploy_hash="abc1234",
        )
    )["Resources"]
    fn = res["DeployHashStripFunctionV"]["Properties"]
    assert "async function handler" in fn["FunctionCode"]
    assert "401" in fn["FunctionCode"]
    assert "KeyValueStoreAssociations" in fn["FunctionConfig"]


def test_without_basic_auth_everything_stays_off():
    """basic_auth なしでは従来と同一 (回帰ガード)"""
    stack = _stack(routes=[RouteContext(is_default=True, is_spa=True)], basic_auth=None)
    res = _doc(stack)["Resources"]
    assert "BasicAuthFunction" not in res
    assert "TokenKvs" not in res
    code = res["UrlFallbackFunctionRoot"]["Properties"]["FunctionCode"]
    assert "async" not in code
    assert "401" not in code


# --- resource (期待ヘッダ値と KVS 書き込み) ---


def _cloudfront_resource(basic_auth="BA"):
    from pocket_cli.resources.cloudfront import CloudFront

    ctx = CloudFrontContext(
        name="web",
        region="ap-northeast-1",
        s3_region="ap-northeast-1",
        stage="dev",
        slug="dev-testprj-web",
        bucket_name="dev-testprj-bucket",
        resource_prefix="dev-testprj-",
        routes=[RouteContext(is_default=True)],
        basic_auth=basic_auth,
    )
    with patch("pocket_cli.resources.cloudfront.boto3"):
        return CloudFront(ctx)


def _mediator_with_secrets(secrets: dict):
    mediator = MagicMock()
    mediator.context.awscontainer.secrets.pocket_store.secrets = secrets
    return mediator


def test_prepare_basic_auth_builds_expected_header():
    cf = _cloudfront_resource()
    cf._prepare_basic_auth(_mediator_with_secrets({"BA": "user:pass"}))
    expected = "Basic %s" % base64.b64encode(b"user:pass").decode()
    assert cf._basic_auth_expected == expected


def test_prepare_basic_auth_warns_on_malformed_value():
    """':' を含まない値 (password type の誤参照等) は警告して書き込まない"""
    cf = _cloudfront_resource()
    cf._prepare_basic_auth(_mediator_with_secrets({"BA": "no-colon"}))
    assert cf._basic_auth_expected == ""


def test_kvs_write_puts_both_keys_with_fresh_etag():
    cf = _cloudfront_resource()
    cf._token_secret_value = "tok"  # noqa: S105 (テスト用ダミー)
    cf._basic_auth_expected = "Basic abc"
    with (
        patch.object(
            type(cf), "stack", new=MagicMock(output={"TokenKvsArn": "arn:kvs"})
        ),
        patch("pocket_cli.resources.cloudfront.boto3") as mock_boto3,
    ):
        kvs = mock_boto3.client.return_value
        kvs.describe_key_value_store.side_effect = [
            {"ETag": "e1"},
            {"ETag": "e2"},
        ]
        cf._write_token_secret_to_kvs()
    puts = {c.kwargs["Key"]: c.kwargs for c in kvs.put_key.call_args_list}
    assert puts["token_secret"]["Value"] == "tok"
    assert puts["basic_auth"]["Value"] == "Basic abc"
    etags = [c.kwargs["IfMatch"] for c in kvs.put_key.call_args_list]
    assert etags == ["e1", "e2"]  # put ごとに describe し直す
