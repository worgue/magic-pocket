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


def test_lambda_route_cannot_use_require_token():
    """type='lambda' では require_token 禁止"""
    with pytest.raises(ValueError, match="require_token"):
        Route.model_validate(
            {
                "type": "lambda",
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


def test_shared_vector_with_rust():
    """Rust 実装 (pocket-spa-auth) との共通テストベクタ。

    トークン形式・HMAC 計算・Cookie 名が両実装で一致することを CI で検出する。
    Rust 側の対になるテストは crates/pocket-spa-auth の test_python_shared_vector。
    """
    import json
    from pathlib import Path

    data = json.loads(
        (Path(__file__).parent / "data" / "spa_auth_vectors.json").read_text()
    )
    assert COOKIE_NAME == data["cookie_name"]
    assert verify_token(data["token"], secret=data["secret_hex"]) == data["user_id"]


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


# --- SpaTokenCookieMiddleware ---


class _FakeResponse:
    """テスト用最小 response。set_cookie / delete_cookie を記録するだけ。"""

    def __init__(self):
        self.cookies: dict = {}
        self.deleted_cookies: list = []

    def set_cookie(self, key, value, **kwargs):
        self.cookies[key] = {"value": value, **kwargs}

    def delete_cookie(self, key, **kwargs):
        self.deleted_cookies.append(key)


class _FakeUser:
    def __init__(self, *, authenticated: bool, pk: str = "42"):
        self.is_authenticated = authenticated
        self.pk = pk


class _FakeRequest:
    def __init__(self, *, user: _FakeUser, cookies: dict | None = None):
        self.user = user
        self.COOKIES = cookies or {}


def _make_middleware(response, *, secret=TEST_SECRET, monkeypatch):
    """SPA_TOKEN_SECRET 環境変数を仕込んだ middleware を返す。"""
    from pocket.django.spa_auth import SpaTokenCookieMiddleware

    monkeypatch.setenv("SPA_TOKEN_SECRET", secret)
    return SpaTokenCookieMiddleware(lambda req: response)


def test_middleware_authenticated_no_cookie_issues_token(monkeypatch):
    """認証済み + cookie なし → token を発行 (redirect loop 防止の核)。"""
    resp = _FakeResponse()
    mw = _make_middleware(resp, monkeypatch=monkeypatch)
    req = _FakeRequest(user=_FakeUser(authenticated=True, pk="42"))
    out = mw(req)
    assert out is resp
    assert COOKIE_NAME in resp.cookies
    assert verify_token(resp.cookies[COOKIE_NAME]["value"]) == "42"


def test_middleware_authenticated_expired_cookie_reissues(monkeypatch):
    """認証済み + 期限切れ cookie → 新 token で上書き発行 (現実シナリオ)。"""
    expired = f"42:{int(time.time()) - 60}:deadbeef"
    resp = _FakeResponse()
    mw = _make_middleware(resp, monkeypatch=monkeypatch)
    req = _FakeRequest(
        user=_FakeUser(authenticated=True, pk="42"),
        cookies={COOKIE_NAME: expired},
    )
    mw(req)
    assert COOKIE_NAME in resp.cookies
    new_token = resp.cookies[COOKIE_NAME]["value"]
    assert new_token != expired
    assert verify_token(new_token) == "42"


def test_middleware_authenticated_valid_cookie_no_change(monkeypatch):
    """認証済み + 有効 cookie → 何もしない (毎リクエスト reset しない)。"""
    monkeypatch.setenv("SPA_TOKEN_SECRET", TEST_SECRET)
    valid = generate_token("42", secret=TEST_SECRET)
    resp = _FakeResponse()
    mw = _make_middleware(resp, monkeypatch=monkeypatch)
    req = _FakeRequest(
        user=_FakeUser(authenticated=True, pk="42"),
        cookies={COOKIE_NAME: valid},
    )
    mw(req)
    assert COOKIE_NAME not in resp.cookies


def test_middleware_unauthenticated_clears_stale_cookie(monkeypatch):
    """未認証 + cookie 残存 → delete (logout 漏れ対策)。"""
    resp = _FakeResponse()
    mw = _make_middleware(resp, monkeypatch=monkeypatch)
    req = _FakeRequest(
        user=_FakeUser(authenticated=False),
        cookies={COOKIE_NAME: "anything"},
    )
    mw(req)
    assert COOKIE_NAME in resp.deleted_cookies


def test_middleware_unauthenticated_no_cookie_noop(monkeypatch):
    resp = _FakeResponse()
    mw = _make_middleware(resp, monkeypatch=monkeypatch)
    req = _FakeRequest(user=_FakeUser(authenticated=False))
    mw(req)
    assert COOKIE_NAME not in resp.cookies
    assert COOKIE_NAME not in resp.deleted_cookies


def test_middleware_no_secret_env_is_noop(monkeypatch):
    """SPA_TOKEN_SECRET 未設定の環境 (gating 未 deploy のローカル等) は no-op。"""
    from pocket.django.spa_auth import SpaTokenCookieMiddleware

    monkeypatch.delenv("SPA_TOKEN_SECRET", raising=False)
    resp = _FakeResponse()
    mw = SpaTokenCookieMiddleware(lambda req: resp)
    req = _FakeRequest(
        user=_FakeUser(authenticated=True, pk="42"),
        cookies={COOKIE_NAME: "anything"},
    )
    out = mw(req)
    assert out is resp
    assert COOKIE_NAME not in resp.cookies
    assert COOKIE_NAME not in resp.deleted_cookies


# --- SpaTokenCookieMiddleware: override hooks ---


def test_middleware_subclass_can_override_should_issue(monkeypatch):
    """subclass で _should_issue を上書きして発行条件を拡張できる。
    sliding refresh 相当のユースケース。"""
    from pocket.django.spa_auth import SpaTokenCookieMiddleware

    class AlwaysIssue(SpaTokenCookieMiddleware):
        def _should_issue(self, request):
            return True

    monkeypatch.setenv("SPA_TOKEN_SECRET", TEST_SECRET)
    valid = generate_token("42", secret=TEST_SECRET)
    resp = _FakeResponse()
    mw = AlwaysIssue(lambda req: resp)
    req = _FakeRequest(
        user=_FakeUser(authenticated=True, pk="42"),
        cookies={COOKIE_NAME: valid},  # 有効 token があっても上書き発行される
    )
    mw(req)
    assert COOKIE_NAME in resp.cookies


def test_middleware_subclass_can_override_max_age(monkeypatch):
    """subclass で _max_age を上書きして短命 token を発行できる。"""
    from pocket.django.spa_auth import SpaTokenCookieMiddleware

    class ShortLived(SpaTokenCookieMiddleware):
        def _max_age(self):
            return 60  # 1 分

    monkeypatch.setenv("SPA_TOKEN_SECRET", TEST_SECRET)
    resp = _FakeResponse()
    mw = ShortLived(lambda req: resp)
    req = _FakeRequest(user=_FakeUser(authenticated=True, pk="42"))
    mw(req)
    assert resp.cookies[COOKIE_NAME]["max_age"] == 60


def test_generate_token_rejects_colon_user_id():
    """user_id に : を含むとトークン形式と衝突して verify で常に無効になるため、
    発行時に reject する (毎レスポンス再発行 + redirect ループの恒久化防止)"""
    with pytest.raises(ValueError, match="user_id"):
        generate_token("tenant:42", secret=TEST_SECRET)


def test_middleware_reissues_on_user_switch(monkeypatch):
    """logout を挟まないアカウント切替時に旧ユーザーの token を上書き発行すること"""
    monkeypatch.setenv("SPA_TOKEN_SECRET", TEST_SECRET)
    old_token = generate_token("41", secret=TEST_SECRET)
    resp = _FakeResponse()
    mw = _make_middleware(resp, monkeypatch=monkeypatch)
    req = _FakeRequest(
        user=_FakeUser(authenticated=True, pk="42"),
        cookies={COOKIE_NAME: old_token},
    )
    mw(req)
    assert COOKIE_NAME in resp.cookies
    assert verify_token(resp.cookies[COOKIE_NAME]["value"]) == "42"


def test_middleware_keeps_valid_token_for_same_user(monkeypatch):
    """同一ユーザーの有効 token は再発行しないこと (従来挙動の維持)"""
    monkeypatch.setenv("SPA_TOKEN_SECRET", TEST_SECRET)
    token = generate_token("42", secret=TEST_SECRET)
    resp = _FakeResponse()
    mw = _make_middleware(resp, monkeypatch=monkeypatch)
    req = _FakeRequest(
        user=_FakeUser(authenticated=True, pk="42"),
        cookies={COOKIE_NAME: token},
    )
    mw(req)
    assert COOKIE_NAME not in resp.cookies
