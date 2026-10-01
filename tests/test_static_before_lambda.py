"""static を Lambda 更新より先に upload する順序のテスト (KN1676)。

Lambda が新しい版の URL を出し始めた後に static を upload すると、その間に
読まれたファイルは upload 前の内容が新しい URL のキャッシュに入り、ブラウザに
versioned_max_age の間残る。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pocket_cli import django_cli
from pocket_cli.cli import deploy_cli


def _route(name: str, versioning: str | None):
    return SimpleNamespace(name=name, versioning=versioning)


def _context(routes):
    cf = SimpleNamespace(name="web", uploadable_routes=routes)
    context = MagicMock()
    context.cloudfront = {"web": cf}
    return context


@pytest.fixture
def fake_cloudfront(monkeypatch):
    """CloudFront resource を差し替え、upload に渡された route 名を記録する。"""
    state = SimpleNamespace(status="COMPLETED", uploads=[])

    class _FakeCloudFront:
        def __init__(self, context):
            self.status = state.status

        def upload(self, *, routes, skip_build=False):
            state.uploads.append([r.name for r in routes])

    monkeypatch.setattr(deploy_cli, "CloudFront", _FakeCloudFront)
    return state


def test_versioned_frontend_uploads_only_versioning_routes(fake_cloudfront):
    context = _context([_route("static", "deploy_hash"), _route("spa", None)])
    uploaded = deploy_cli.deploy_versioned_frontend(context)
    assert fake_cloudfront.uploads == [["static"]]
    assert uploaded == {("web", "static")}
    # 残り (versioning の無い route) は Lambda 更新の後に upload する
    deploy_cli.deploy_frontend(context, exclude=uploaded)
    assert fake_cloudfront.uploads == [["static"], ["spa"]]


def test_versioned_frontend_defers_when_cloudfront_missing(fake_cloudfront):
    """初回 deploy (CloudFront 未作成) では先に upload せず、後段に任せること"""
    fake_cloudfront.status = "NOEXIST"
    context = _context([_route("static", "deploy_hash")])
    assert deploy_cli.deploy_versioned_frontend(context) == frozenset()
    assert fake_cloudfront.uploads == []


def test_pipeline_uploads_static_before_lambda_update(monkeypatch):
    calls: list[str] = []
    uploaded = frozenset({("web", "static")})

    def record(name, result=None):
        def _fn(*args, **kwargs):
            calls.append(name)
            return result

        return _fn

    for name in [
        "verify_stage_roles",
        "check_removed_inbound",
        "cleanup_unused_inbound_domains",
        "upload_managed_assets",
        "resummarize_world_read_warnings",
        "echo_dead_letter_alert_warnings",
        "echo_inbound_details",
        "confirm_inbound_domains",
    ]:
        monkeypatch.setattr(deploy_cli, name, lambda *a, **k: None)
    monkeypatch.setattr(deploy_cli, "deploy_hash_report", lambda c: None)
    monkeypatch.setattr(deploy_cli, "_get_deploy_url", lambda c: None)
    monkeypatch.setattr(deploy_cli, "_create_state_store", lambda c: MagicMock())
    monkeypatch.setattr(deploy_cli.migrations, "run_deploy_cleanup", lambda c: None)
    monkeypatch.setattr(deploy_cli, "deploy_init_resources", record("init"))
    monkeypatch.setattr(deploy_cli, "deploy_resources", record("resources"))
    monkeypatch.setattr(
        deploy_cli, "deploy_versioned_frontend", record("versioned", uploaded)
    )
    seen_exclude: list[frozenset] = []

    def fake_deploy_frontend(context, *, exclude):
        calls.append("frontend")
        seen_exclude.append(exclude)

    monkeypatch.setattr(deploy_cli, "deploy_frontend", fake_deploy_frontend)

    context = MagicMock()
    context.inbound = {}
    context.inbound_domain = {}
    deploy_cli._deploy_pipeline(
        context,
        before_lambda_update=lambda: calls.append("hook"),
    )
    assert calls == ["init", "versioned", "hook", "resources", "frontend"]
    assert seen_exclude == [uploaded]


@pytest.fixture
def django_static_env(monkeypatch):
    """_deploystatic_before_lambda_update の外部依存を差し替える。"""
    state = SimpleNamespace(
        publish="deploy", store="s3", bucket_exists=True, confirm=True, calls=[]
    )

    def fake_from_toml(cls, stage):
        return SimpleNamespace(s3=object())

    def fake_container(context):
        storage = SimpleNamespace(store=state.store, publish=state.publish, link=False)
        return SimpleNamespace(
            django=SimpleNamespace(storages={"staticfiles": storage})
        )

    class _FakeS3:
        def __init__(self, context):
            pass

        def exists(self):
            return state.bucket_exists

    monkeypatch.setattr(django_cli.Context, "from_toml", classmethod(fake_from_toml))
    monkeypatch.setattr(django_cli, "resolve_django_container", fake_container)
    monkeypatch.setattr(django_cli, "S3", _FakeS3)
    monkeypatch.setattr(
        django_cli.interaction, "confirm", lambda *a, **k: state.confirm
    )
    monkeypatch.setattr(
        django_cli,
        "collectstatic_locally",
        lambda stage, link=False: state.calls.append("collect"),
    )
    monkeypatch.setattr(
        django_cli,
        "upload_collected_staticfiles",
        lambda stage: state.calls.append("upload"),
    )
    return state


def test_django_static_is_published_before_lambda_update(django_static_env):
    assert django_cli._deploystatic_before_lambda_update("dev") is True
    assert django_static_env.calls == ["collect", "upload"]


def test_django_static_declined_is_not_asked_again(django_static_env):
    """確認に No と答えた場合も「済み」とし、deploy 後にもう一度聞かないこと"""
    django_static_env.confirm = False
    assert django_cli._deploystatic_before_lambda_update("dev") is True
    assert django_static_env.calls == []


@pytest.mark.parametrize(
    "override",
    [
        {"bucket_exists": False},  # 初回 deploy
        {"publish": "command"},
        {"store": "filesystem"},
    ],
)
def test_django_static_defers_to_post_deploy(django_static_env, override):
    for key, value in override.items():
        setattr(django_static_env, key, value)
    assert django_cli._deploystatic_before_lambda_update("dev") is False
    assert django_static_env.calls == []


def test_post_deploy_skips_static_when_already_handled(monkeypatch):
    confirms: list[str] = []
    monkeypatch.setattr(
        django_cli.interaction,
        "confirm",
        lambda message, default=True: confirms.append(message) or False,
    )
    monkeypatch.setattr(django_cli.interaction, "set_assume_yes", lambda v: None)
    monkeypatch.setattr(
        django_cli.Context, "from_toml", classmethod(lambda cls, stage: object())
    )
    monkeypatch.setattr("pocket_cli.cli.deploy_cli._get_deploy_url", lambda ctx: None)
    django_cli._django_post_deploy(
        "dev", yes=False, openpath=None, skip_migrate=True, static_handled=True
    )
    assert not any("deploystatic" in c for c in confirms)
