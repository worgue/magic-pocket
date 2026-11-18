"""resource destroy サブコマンドのガード (共有 ECR / 確認プロンプト) のテスト。"""

from __future__ import annotations

from click.testing import CliRunner
from pocket_cli.cli import container_cli, destroy_cli
from pocket_cli.cli.cloudfront_cli import cloudfront
from pocket_cli.cli.neon_cli import neon
from pocket_cli.cli.tidb_cli import tidb
from pocket_cli.cli.upstash_cli import upstash


def test_awscontainer_destroy_uses_shared_teardown(monkeypatch, use_toml):
    """resource awscontainer destroy がトップレベルと同じ実装を使うこと

    別実装だと ecr_name_overridden ガード (共有 ECR の削除拒否) が抜け、
    build once 運用で全 stage の image が消える (回帰テスト)。
    """
    use_toml("tests/data/toml/rds.toml")
    calls: list[str] = []
    monkeypatch.setattr(
        container_cli,
        "_collect_container_targets",
        lambda c, w: ["AwsContainer (CFNスタック)"],
    )
    monkeypatch.setattr(
        container_cli,
        "_destroy_containers",
        lambda c, w: calls.append("destroy"),
    )
    runner = CliRunner()
    # 確認拒否 → 削除されない
    result = runner.invoke(
        container_cli.container, ["destroy", "--stage", "dev"], input="n\n"
    )
    assert result.exit_code != 0
    assert calls == []
    # -y → 削除される
    result = runner.invoke(container_cli.container, ["destroy", "--stage", "dev", "-y"])
    assert result.exit_code == 0, result.output
    assert calls == ["destroy"]


def test_top_level_destroy_skips_overridden_ecr(use_toml, monkeypatch):
    """ecr_name 明示指定 (共有の可能性) の ECR は削除しないこと"""
    from pocket.context import Context

    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.container["main"] is not None

    deleted = {"ecr": False}

    class _FakeEcr:
        def exists(self):
            return True

        def delete(self):
            deleted["ecr"] = True

    class _FakeStack:
        status = "NOEXIST"

    class _FakeAc:
        stack = _FakeStack()
        ecr = _FakeEcr()

    monkeypatch.setattr(destroy_cli, "Container", lambda ctx: _FakeAc())
    monkeypatch.setattr(destroy_cli, "_destroy_codebuild", lambda c: None)
    monkeypatch.setattr(destroy_cli, "_destroy_log_groups", lambda c: None)
    monkeypatch.setattr(destroy_cli, "_destroy_legacy_containers", lambda c: None)
    ac_ctx = context.container["main"].model_copy(update={"ecr_name_overridden": True})
    guarded = context.model_copy(update={"container": {"main": ac_ctx}})
    destroy_cli._destroy_containers(guarded, with_secrets=False)
    assert deleted["ecr"] is False


def _assert_confirm_guard(runner, group, args):
    result = runner.invoke(group, args, input="n\n")
    assert result.exit_code != 0, "確認拒否で中断されること: %s" % (args,)


def test_destroy_subcommands_require_confirmation(monkeypatch):
    """破壊系サブコマンドが確認なしで削除に進まないこと"""
    runner = CliRunner()
    # リソース取得より前に confirm が走るコマンドはそのまま呼べる
    _assert_confirm_guard(runner, cloudfront, ["destroy", "--stage", "dev"])
    _assert_confirm_guard(runner, neon, ["delete", "--stage", "dev"])
    _assert_confirm_guard(runner, tidb, ["delete", "--stage", "dev"])
    _assert_confirm_guard(runner, upstash, ["delete", "--stage", "dev"])


def test_collect_database_targets_skips_provider_databases(use_toml):
    """Neon / TiDB / Upstash は削除対象の一覧に載らないこと"""
    from pocket.context import Context

    use_toml("tests/data/toml/db_command_provisioning.toml")
    context = Context.from_toml(stage="dev")
    assert destroy_cli._collect_database_targets(context) == []


def test_destroy_resources_only_warns_about_provider_databases(
    use_toml, monkeypatch, capsys
):
    """destroy は provider の DB を消さず、消し方を案内するだけ (API key 不要)。"""
    from pocket.context import Context

    use_toml("tests/data/toml/db_command_provisioning.toml")
    context = Context.from_toml(stage="dev")

    monkeypatch.setattr(destroy_cli, "_destroy_cloudfront_and_acm", lambda c: None)
    monkeypatch.setattr(destroy_cli, "_destroy_containers", lambda c, w: None)
    monkeypatch.setattr(destroy_cli, "_destroy_dsql", lambda c: None)
    monkeypatch.setattr(destroy_cli, "_destroy_rds", lambda c: None)
    monkeypatch.setattr(destroy_cli, "_destroy_vpc", lambda c: None)

    destroy_cli._destroy_resources(context, with_secrets=False, with_state_bucket=False)
    output = "".join(capsys.readouterr())
    for command in (
        "pocket resource neon delete",
        "pocket resource tidb delete",
        "pocket resource upstash delete",
    ):
        assert command in output
