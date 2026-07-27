"""廃止済み CLI フラグの移行案内テスト。

`--skip-check-existing` (0.6.0 で廃止) を渡すと、click の "No such option" では
なく provisioning="command" + store-url への移行手順を示してエラーになること。
旧バージョンからの一括アップデートで CI がガイドなしに停止した実害への回帰テスト
(2026-07-24 受領の利用プロジェクト feedback)。
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner
from pocket_cli.cli.deploy_cli import deploy, promote
from pocket_cli.django_cli import django


@pytest.mark.parametrize(
    ("cli", "args"),
    [
        (deploy, ["--stage", "dev", "--skip-check-existing"]),
        (promote, ["--stage", "dev", "--commit-hash", "abc", "--skip-check-existing"]),
        (django, ["deploy", "--stage", "dev", "--skip-check-existing"]),
        (
            django,
            [
                "promote",
                "--stage",
                "dev",
                "--commit-hash",
                "abc",
                "--skip-check-existing",
            ],
        ),
    ],
)
def test_skip_check_existing_fails_with_migration_guide(cli, args):
    result = CliRunner().invoke(cli, args)
    assert result.exit_code != 0
    out = result.output.replace("\n", "")
    assert "廃止" in out
    assert "store-url" in out
    assert "No such option" not in result.output


def test_skip_check_existing_is_hidden_from_help():
    """廃止フラグは --help には表示しない (移行案内専用の hidden オプション)"""
    result = CliRunner().invoke(deploy, ["--help"])
    assert result.exit_code == 0
    assert "skip-check-existing" not in result.output
