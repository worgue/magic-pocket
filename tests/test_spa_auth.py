import time

import pytest
from moto import mock_aws

from pocket.context import Context
from pocket.django.spa_auth import (
    COOKIE_NAME,
    generate_token,
    spa_login,
    spa_logout,
    verify_token,
)
from pocket.settings import CloudFront, Route, Settings

TEST_SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


# --- settings バリデーション ---


def test_require_token_requires_is_spa():
    """require_token=True には is_spa=True が必須"""
    with pytest.raises(ValueError, match="require_token=True requires is_spa=True"):
        Route.model_validate(
            {
                "is_default": True,
                "require_token": True,
                "origin_path": "/app",
            }
        )


def test_require_token_with_is_spa_ok():
    """require_token=True + is_spa=True は有効"""
    route = Route.model_validate(
        {
            "is_default": True,
            "is_spa": True,
            "require_token": True,
            "origin_path": "/app",
        }
    )
    assert route.require_token is True
    assert route.is_spa is True


def test_api_route_cannot_use_require_token():
    """type='api' では require_token 禁止"""
    with pytest.raises(ValueError, match="require_token"):
        Route.model_validate(
            {
                "type": "api",
                "handler": "wsgi",
                "path_pattern": "/api/*",
                "require_token": True,
                "is_spa": True,
            }
        )


def test_token_secret_required_when_require_token():
    """require_token ルートがあるのに token_secret 未設定 → エラー"""
    with pytest.raises(
        ValueError, match="token_secret is required when route has require_token=true"
    ):
        CloudFront.model_validate(
            {
                "routes": [
                    {
                        "is_default": True,
                        "is_spa": True,
                        "require_token": True,
                        "origin_path": "/app",
                    },
                ],
            }
        )


def test_token_secret_needs_managed_secret():
    """token_secret が managed secrets に存在しない → エラー"""
    with pytest.raises(ValueError, match="token_secret.*not found"):
        Settings.model_validate(
            {
                "stage": "dev",
                "general": {
                    "region": "ap-southeast-1",
                    "project_name": "testprj",
                    "stages": ["dev"],
                },
                "s3": {},
                "awscontainer": {
                    "dockerfile_path": "Dockerfile",
                    "handlers": {
                        "wsgi": {
                            "command": "pocket.django.lambda_handlers.wsgi_handler"
                        }
                    },
                    "secrets": {"managed": {}},
                },
                "cloudfront": {
                    "main": {
                        "token_secret": "NONEXISTENT",
                        "routes": [
                            {
                                "is_default": True,
                                "is_spa": True,
                                "require_token": True,
                                "origin_path": "/app",
                            },
                        ],
                    }
                },
            }
        )


# --- context ---


@mock_aws
def test_spa_token_context(use_toml):
    """token_secret と require_token が context に反映される"""
    use_toml("tests/data/toml/cloudfront_spa_token.toml")
    context = Context.from_toml(stage="dev")
    assert context.cloudfront
    cf = context.cloudfront["main"]
    assert cf.token_secret == "SPA_TOKEN_SECRET"
    assert cf.default_route.require_token is True
    assert cf.default_route.login_path == "/api/auth/login"


# --- Django ヘルパー ---


def test_generate_and_verify_token():
    """トークンの生成と検証"""
    token = generate_token("user42", secret=TEST_SECRET, max_age=3600)
    result = verify_token(token, secret=TEST_SECRET)
    assert result == "user42"


def test_verify_expired_token():
    """期限切れトークンは None"""
    token = generate_token("user42", secret=TEST_SECRET, max_age=0)
    # max_age=0 → expiry == now。少し待って期限切れにする
    time.sleep(1.1)
    result = verify_token(token, secret=TEST_SECRET)
    assert result is None


def test_verify_invalid_signature():
    """改ざんされた署名は None"""
    token = generate_token("user42", secret=TEST_SECRET)
    parts = token.split(":")
    tampered = f"{parts[0]}:{parts[1]}:deadbeef"
    result = verify_token(tampered, secret=TEST_SECRET)
    assert result is None


def test_verify_malformed_token():
    """不正形式のトークンは None"""
    assert verify_token("invalid", secret=TEST_SECRET) is None
    assert verify_token("a:b", secret=TEST_SECRET) is None
    assert verify_token("", secret=TEST_SECRET) is None


def test_spa_login_sets_cookie():
    """spa_login がレスポンスに Cookie をセットする"""

    class FakeResponse:
        def __init__(self):
            self.cookies: dict = {}

        def set_cookie(self, key, value, **kwargs):
            self.cookies[key] = {"value": value, **kwargs}

    resp = FakeResponse()
    spa_login(resp, "user42", secret=TEST_SECRET, max_age=3600)
    assert COOKIE_NAME in resp.cookies
    cookie = resp.cookies[COOKIE_NAME]
    assert cookie["max_age"] == 3600
    assert cookie["httponly"] is True
    assert cookie["secure"] is True
    # Cookie 値がトークンとして検証可能
    result = verify_token(cookie["value"], secret=TEST_SECRET)
    assert result == "user42"


def test_spa_logout_deletes_cookie():
    """spa_logout が Cookie を削除する"""

    class FakeResponse:
        def __init__(self):
            self.deleted_cookies: list = []

        def delete_cookie(self, key, **kwargs):
            self.deleted_cookies.append(key)

    resp = FakeResponse()
    spa_logout(resp)
    assert COOKIE_NAME in resp.deleted_cookies
