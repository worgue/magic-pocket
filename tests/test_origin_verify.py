"""origin verify (詐称耐性 client IP + origin 直叩き防止) 関連のテスト。

- pocket.toml schema: [cloudfront.<name>].enable_origin_verify と validation
- context: 有効時に managed secret POCKET_ORIGIN_VERIFY_SECRET が自動注入される
- mediator: origin_verify_secret 型の生成
- CFn template: OriginCustomHeaders / viewer IP header が出る/出ない
- client_ip パーサ: IPv4 / IPv6 / port 付きの各形式
- OriginVerifyMiddleware: no-op / 403 / REMOTE_ADDR 正規化
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
from pocket_cli.mediator import Mediator
from pocket_cli.resources.aws.cloudformation import CloudFrontStack
from pydantic import ValidationError

from pocket import settings
from pocket.context import (
    ORIGIN_VERIFY_SECRET_KEY,
    CloudFrontContext,
    Context,
    RouteContext,
)
from pocket.django.client_ip import parse_viewer_ip
from pocket.django.origin_verify import (
    ORIGIN_VERIFY_HEADER_META,
    ORIGIN_VERIFY_SECRET_ENV,
    VIEWER_IP_HEADER_META,
    OriginVerifyMiddleware,
)
from pocket.settings import CloudFront as CloudFrontSettings


# 一致していないと middleware が env から secret を読めない / context の注入キーと
# ずれると検証できない。drift 防止のための明示アサート。
def test_secret_env_name_matches_managed_key():
    assert ORIGIN_VERIFY_SECRET_ENV == ORIGIN_VERIFY_SECRET_KEY


# ---------------------------------------------------------------------------
# settings schema
# ---------------------------------------------------------------------------


def test_enable_origin_verify_defaults_false():
    cf = CloudFrontSettings.model_validate(
        {"routes": [{"is_default": True, "is_spa": True, "origin_path": "/main"}]}
    )
    assert cf.enable_origin_verify is False


def _settings_dict(*, enable: bool, lambda_route: bool) -> dict:
    if lambda_route:
        route = {"type": "lambda", "handler": "wsgi", "is_default": True}
    else:
        route = {"is_default": True, "is_spa": True, "origin_path": "/main"}
    return {
        "stage": "dev",
        "general": {
            "region": "ap-northeast-1",
            "project_name": "testprj",
            "stages": ["dev"],
        },
        "s3": {},
        "awscontainer": {
            "dockerfile_path": "Dockerfile",
            "handlers": {
                "wsgi": {
                    "command": "pocket.django.lambda_handlers.wsgi_handler",
                    "apigateway": {},
                }
            },
        },
        "cloudfront": {"web": {"enable_origin_verify": enable, "routes": [route]}},
    }


def test_enable_origin_verify_requires_lambda_route():
    """lambda origin の無い (S3 のみ) 構成で enable_origin_verify は reject。"""
    data = _settings_dict(enable=True, lambda_route=False)
    with pytest.raises(ValidationError) as exc:
        settings.Settings.model_validate(data)
    assert "lambda route" in str(exc.value)


def test_enable_origin_verify_accepts_lambda_route():
    s = settings.Settings.model_validate(_settings_dict(enable=True, lambda_route=True))
    assert s.cloudfront["web"].enable_origin_verify is True


# ---------------------------------------------------------------------------
# context: managed secret の自動注入
# ---------------------------------------------------------------------------


def test_context_injects_managed_secret_when_enabled():
    s = settings.Settings.model_validate(_settings_dict(enable=True, lambda_route=True))
    context = Context.from_settings(s)
    assert context.awscontainer is not None
    assert context.awscontainer.secrets is not None
    managed = context.awscontainer.secrets.managed
    assert ORIGIN_VERIFY_SECRET_KEY in managed
    assert managed[ORIGIN_VERIFY_SECRET_KEY].type == "origin_verify_secret"
    assert context.cloudfront["web"].enable_origin_verify is True


def test_context_no_managed_secret_when_disabled():
    s = settings.Settings.model_validate(
        _settings_dict(enable=False, lambda_route=True)
    )
    context = Context.from_settings(s)
    assert context.awscontainer is not None
    # secrets を宣言していないので origin verify 無効なら secrets context も作られない
    assert context.awscontainer.secrets is None
    assert context.cloudfront["web"].enable_origin_verify is False


# ---------------------------------------------------------------------------
# mediator
# ---------------------------------------------------------------------------


def test_mediator_generates_origin_verify_secret(base_settings):
    mediator = Mediator(Context.from_settings(base_settings))
    spec = settings.ManagedSecretSpec(type="origin_verify_secret")
    value = mediator._generate_secret(spec)
    assert isinstance(value, str)
    # token_hex(32) = 64 hex chars
    assert len(value) == 64
    int(value, 16)  # hex として valid


# ---------------------------------------------------------------------------
# CFn template
# ---------------------------------------------------------------------------


def _cf_lambda_context(*, enable_origin_verify: bool = True) -> CloudFrontContext:
    return CloudFrontContext(
        name="web",
        region="ap-northeast-1",
        s3_region="ap-northeast-1",
        stage="dev",
        slug="dev-testprj-web",
        bucket_name="dev-testprj-bucket",
        resource_prefix="dev-testprj-",
        routes=[RouteContext(type="lambda", handler="wsgi", is_default=True)],
        api_origins={"wsgi": "dev-testprj-wsgi-api-domain"},
        enable_origin_verify=enable_origin_verify,
    )


def test_template_includes_origin_custom_header_when_secret_present():
    ctx = _cf_lambda_context()
    with mock.patch("boto3.client"):
        yaml = CloudFrontStack(ctx, origin_verify_secret_value="deadbeefsecret").yaml
    assert "OriginCustomHeaders" in yaml
    assert "X-Pocket-Origin-Verify" in yaml
    assert "deadbeefsecret" in yaml


def test_template_omits_origin_custom_header_without_secret():
    ctx = _cf_lambda_context(enable_origin_verify=False)
    with mock.patch("boto3.client"):
        yaml = CloudFrontStack(ctx, origin_verify_secret_value="").yaml
    assert "OriginCustomHeaders" not in yaml
    assert "X-Pocket-Origin-Verify" not in yaml


def test_api_host_function_injects_viewer_ip():
    """viewer IP 転送は flag 非依存で lambda route に常時入る。"""
    ctx = _cf_lambda_context(enable_origin_verify=False)
    with mock.patch("boto3.client"):
        yaml = CloudFrontStack(ctx, origin_verify_secret_value="").yaml
    assert "x-pocket-viewer-ip" in yaml
    assert "event.viewer.ip" in yaml


# ---------------------------------------------------------------------------
# client_ip パーサ
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("198.51.100.10", "198.51.100.10"),
        ("198.51.100.10:443", "198.51.100.10"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:8080", "2001:db8::1"),
        ("2001:db8::1:60776", "2001:db8::1"),  # CloudFront 非標準 (角括弧なし+port)
        ("  198.51.100.10  ", "198.51.100.10"),
        ("", None),
        ("not-an-ip", None),
        ("999.999.999.999", None),
    ],
)
def test_parse_viewer_ip(raw, expected):
    assert parse_viewer_ip(raw) == expected


# ---------------------------------------------------------------------------
# OriginVerifyMiddleware
# ---------------------------------------------------------------------------


def _ensure_django_configured():
    """403 経路 (HttpResponseForbidden) は Django settings を参照するため、
    未設定なら最小設定で configure する (本番では Django が設定済み)。"""
    from django.conf import settings as dj_settings

    if not dj_settings.configured:
        dj_settings.configure(DEFAULT_CHARSET="utf-8", ALLOWED_HOSTS=["*"])


TEST_SECRET = "a" * 64  # noqa: S105 テスト用ダミー


class _FakeRequest:
    def __init__(self, meta: dict | None = None):
        self.META = meta or {}


def _make_mw(monkeypatch, *, secret: str | None):
    sentinel = object()

    def get_response(request):
        request._reached = sentinel
        return sentinel

    if secret is None:
        monkeypatch.delenv(ORIGIN_VERIFY_SECRET_ENV, raising=False)
    else:
        monkeypatch.setenv(ORIGIN_VERIFY_SECRET_ENV, secret)
    return OriginVerifyMiddleware(get_response), sentinel


def test_mw_noop_when_disabled(monkeypatch):
    """env secret 未設定 (local/dev) → passthrough。REMOTE_ADDR は触らない。"""
    mw, sentinel = _make_mw(monkeypatch, secret=None)
    req = _FakeRequest(
        {
            "REMOTE_ADDR": "10.0.0.1",
            VIEWER_IP_HEADER_META: "198.51.100.10",
        }
    )
    out = mw(req)
    assert out is sentinel
    assert req.META["REMOTE_ADDR"] == "10.0.0.1"


def test_mw_normalizes_remote_addr_when_secret_matches(monkeypatch):
    mw, sentinel = _make_mw(monkeypatch, secret=TEST_SECRET)
    req = _FakeRequest(
        {
            "REMOTE_ADDR": "10.0.0.1",
            ORIGIN_VERIFY_HEADER_META: TEST_SECRET,
            VIEWER_IP_HEADER_META: "198.51.100.10:54321",
        }
    )
    out = mw(req)
    assert out is sentinel
    assert req.META["REMOTE_ADDR"] == "198.51.100.10"


def test_mw_forbids_when_secret_missing(monkeypatch):
    _ensure_django_configured()
    mw, sentinel = _make_mw(monkeypatch, secret=TEST_SECRET)
    req = _FakeRequest({"REMOTE_ADDR": "10.0.0.1"})
    out = mw(req)
    assert out is not sentinel
    assert out.status_code == 403
    assert not hasattr(req, "_reached")


def test_mw_forbids_when_secret_mismatch(monkeypatch):
    _ensure_django_configured()
    mw, sentinel = _make_mw(monkeypatch, secret=TEST_SECRET)
    req = _FakeRequest({ORIGIN_VERIFY_HEADER_META: "b" * 64})
    out = mw(req)
    assert out.status_code == 403


def test_mw_passthrough_when_match_but_no_viewer_ip(monkeypatch):
    """secret 一致だが viewer IP header が無い → REMOTE_ADDR は維持して通す。"""
    mw, sentinel = _make_mw(monkeypatch, secret=TEST_SECRET)
    req = _FakeRequest(
        {"REMOTE_ADDR": "10.0.0.1", ORIGIN_VERIFY_HEADER_META: TEST_SECRET}
    )
    out = mw(req)
    assert out is sentinel
    assert req.META["REMOTE_ADDR"] == "10.0.0.1"


def test_prepare_deploy_loads_secret_for_consistent_hash():
    """prepare_deploy 後の stack hash が update 時 (secret 込み) と一致すること

    fresh インスタンスの空 secret で hash を計算すると deploy 済み hash と
    永遠に一致せず REQUIRE_UPDATE が続き、ensure_post_deploy_state の安全網も
    無効化される (回帰テスト)。
    """
    from pocket_cli.resources.cloudfront import CloudFront

    ctx = _cf_lambda_context()
    store = type(
        "Store", (), {"secrets": {ORIGIN_VERIFY_SECRET_KEY: "deadbeefsecret"}}
    )()
    secrets_ctx = type("Sc", (), {"pocket_store": store})()
    ac = type("Ac", (), {"secrets": secrets_ctx})()
    m_context = type("Mc", (), {"awscontainer": ac})()
    mediator: Any = type("M", (), {"context": m_context})()

    with mock.patch("boto3.client"):
        cf = CloudFront(ctx)
        cf.prepare_deploy(mediator)
        prepared_hash = cf.stack._template_hash
        update_hash = CloudFrontStack(
            ctx, origin_verify_secret_value="deadbeefsecret"
        )._template_hash

    assert cf._origin_verify_secret_value == "deadbeefsecret"
    assert prepared_hash == update_hash


def test_mw_forbids_non_ascii_header_without_500(monkeypatch):
    """非 ASCII のヘッダ値でも TypeError (500) にならず 403 で弾くこと

    Django はヘッダ値を latin-1 decode した str で META に入れるため、
    0x80 以上のバイトを含む直叩きで compare_digest が TypeError を投げていた。
    """
    _ensure_django_configured()
    mw, sentinel = _make_mw(monkeypatch, secret=TEST_SECRET)
    req = _FakeRequest({ORIGIN_VERIFY_HEADER_META: "\xc3\xa9" * 10})
    out = mw(req)
    assert out is not sentinel
    assert out.status_code == 403
