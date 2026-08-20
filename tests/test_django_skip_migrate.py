"""`pocket django deploy/promote --skip-migrate` のテスト (KN1100)。

`-y` は「聞かれたことに全部 yes」の意味を保つため、非対話で migrate だけを
外す手段はフラグ側に置く。DB が到達不能でもインフラ更新は通したい、という
状況が実在する (TiDB Serverless のアクセス制限中に踏んだ)。
"""

from __future__ import annotations

from unittest import mock

import pytest
from pocket_cli import django_cli


@pytest.fixture
def post_deploy_env(monkeypatch):
    """_django_post_deploy の外部依存をすべて差し替え、呼び出しを記録する。"""
    confirms: list[str] = []
    calls: dict[str, object] = {"confirms": confirms, "invoked": False}

    def fake_confirm(message, default=True):
        confirms.append(message)
        return True

    class _FakeHandler:
        def invoke(self, payload):
            calls["invoked"] = True
            return {}

        def show_logs(self, res):
            pass

    monkeypatch.setattr(django_cli.interaction, "confirm", fake_confirm)
    monkeypatch.setattr(django_cli.interaction, "set_assume_yes", lambda v: None)
    monkeypatch.setattr(
        django_cli.Context, "from_toml", classmethod(lambda cls, stage: object())
    )
    monkeypatch.setattr(django_cli, "_staticfiles_publish_mode", lambda ctx: "command")
    monkeypatch.setattr(
        django_cli, "_get_management_command_handler", lambda ctx: _FakeHandler()
    )
    monkeypatch.setattr("pocket_cli.cli.deploy_cli._get_deploy_url", lambda ctx: None)
    return calls


def test_skip_migrate_suppresses_question_and_invoke(post_deploy_env):
    django_cli._django_post_deploy(
        "sandbox", yes=True, openpath=None, skip_migrate=True
    )
    assert post_deploy_env["invoked"] is False
    # 質問自体も出さない
    assert not any("migrate" in c for c in post_deploy_env["confirms"])


def test_without_flag_migrate_still_runs(post_deploy_env):
    django_cli._django_post_deploy(
        "sandbox", yes=True, openpath=None, skip_migrate=False
    )
    assert post_deploy_env["invoked"] is True
    assert any("migrate" in c for c in post_deploy_env["confirms"])


@pytest.mark.parametrize("command_name", ["deploy", "promote"])
def test_both_commands_expose_skip_migrate(command_name):
    """deploy / promote は同じ post-deploy を通るので両方にフラグが要る。"""
    command = django_cli.django.commands[command_name]
    names = {p.name for p in command.params}
    assert "skip_migrate" in names


@pytest.mark.parametrize("command_name", ["deploy", "promote"])
def test_flag_is_threaded_to_post_deploy(command_name, monkeypatch):
    """CLI から渡した --skip-migrate が _django_post_deploy まで届くこと。"""
    seen: dict[str, object] = {}

    def fake_post_deploy(stage, *, yes, openpath, skip_migrate=False):
        seen["skip_migrate"] = skip_migrate

    monkeypatch.setattr(django_cli, "_django_post_deploy", fake_post_deploy)
    monkeypatch.setattr(django_cli.click.Context, "invoke", lambda self, *a, **k: None)

    command = django_cli.django.commands[command_name]
    callback = command.callback
    assert callback is not None
    kwargs = {"stage": "sandbox", "openpath": None, "yes": True, "skip_migrate": True}
    if command_name == "promote":
        kwargs["commit_hash"] = "abc1234"
    with mock.patch.dict("os.environ", {}, clear=False):
        callback(**kwargs)

    assert seen["skip_migrate"] is True
