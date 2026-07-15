"""tidb CLI の破壊的操作ガードのテスト。

TiDB serverless は password reveal API が無く、database_url の算出は
root password のローテーションを伴う。読み取りに見えるコマンドが
意図せず rotate しないことを検証する。
"""

from __future__ import annotations

import click
from click.testing import CliRunner
from pocket_cli.cli import tidb_cli, url_helper


class _FakeCluster:
    name = "test-cluster"
    status = "ACTIVE"
    host = "gateway.test.tidbcloud.example"
    port = 4000
    user = "root.prefix"


class _FakeTiDb:
    project = object()
    cluster = _FakeCluster()

    @property
    def database_url(self):
        raise AssertionError(
            "status は database_url を参照してはいけない"
            " (root password が rotate される)"
        )


def test_tidb_status_does_not_rotate_password(monkeypatch):
    """status コマンドが database_url (= password rotate) に触れないこと"""
    monkeypatch.setattr(tidb_cli, "get_tidb_resource", lambda stage: _FakeTiDb())
    runner = CliRunner()
    result = runner.invoke(tidb_cli.tidb, ["status", "--stage", "dev"])
    assert result.exit_code == 0, result.output
    assert "gateway.test.tidbcloud.example:4000" in result.output


def _make_url_cmd(live_url_calls: list[int]):
    @click.command()
    def cmd():
        def live_url(context):
            live_url_calls.append(1)
            return "mysql://root:pw@h:4000/db"

        url_helper.run_get_url(
            stage="dev",
            secret_type="tidb_database_url",  # noqa: S106 (secret type 名)
            db_label="TiDB",
            live_url=live_url,
            live=False,
            live_rotates_credentials=True,
        )

    return cmd


def test_run_get_url_destructive_fallback_aborts_without_confirmation(monkeypatch):
    """stored 未 provision → 破壊的 live fallback は確認拒否で中断されること"""

    class _FakeContext:
        @classmethod
        def from_toml(cls, *, stage):
            return cls()

    monkeypatch.setattr(url_helper, "Context", _FakeContext)
    monkeypatch.setattr(url_helper, "_read_stored_url", lambda c, t: None)

    calls: list[int] = []
    runner = CliRunner()
    result = runner.invoke(_make_url_cmd(calls), input="n\n")
    assert result.exit_code != 0
    assert calls == []


def test_run_get_url_destructive_fallback_proceeds_with_confirmation(monkeypatch):
    """確認に y で応答すれば live 算出が実行されること"""

    class _FakeContext:
        @classmethod
        def from_toml(cls, *, stage):
            return cls()

    monkeypatch.setattr(url_helper, "Context", _FakeContext)
    monkeypatch.setattr(url_helper, "_read_stored_url", lambda c, t: None)

    calls: list[int] = []
    runner = CliRunner()
    result = runner.invoke(_make_url_cmd(calls), input="y\n")
    assert result.exit_code == 0, result.output
    assert calls == [1]
    assert "mysql://root:pw@h:4000/db" in result.output
