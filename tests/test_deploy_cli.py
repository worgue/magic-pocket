"""deploy_resources の status 処理のテスト。

FAILED / PROGRESS のリソースを「already the latest version」と成功風に
スキップして exit 0 になる false green の回帰テスト。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pocket_cli.cli import deploy_cli

from pocket.context import Context


class _FakeResource:
    def __init__(self, status: str):
        self.status = status

    def state_info(self):
        return {}


def _run_deploy(monkeypatch, context, resource):
    monkeypatch.setattr(deploy_cli, "_create_state_store", lambda c: MagicMock())
    monkeypatch.setattr(deploy_cli, "Mediator", lambda c: MagicMock())
    monkeypatch.setattr(
        deploy_cli, "get_resources", lambda c, state_bucket="": [resource]
    )
    deploy_cli.deploy_resources(context)


def test_deploy_resources_raises_on_failed_resource(use_toml, monkeypatch):
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    with pytest.raises(RuntimeError, match="FAILED"):
        _run_deploy(monkeypatch, context, _FakeResource("FAILED"))


def test_deploy_resources_raises_on_progress_resource(use_toml, monkeypatch):
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    with pytest.raises(RuntimeError, match="進行中"):
        _run_deploy(monkeypatch, context, _FakeResource("PROGRESS"))


def test_deploy_resources_skips_completed_resource(use_toml, monkeypatch):
    """COMPLETED は従来どおり no-op で正常終了すること"""
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    _run_deploy(monkeypatch, context, _FakeResource("COMPLETED"))
