"""`pocket static` は Rust 版 `pocket-rs static` への案内だけを出して exit 2 する。"""

import pytest
from click.testing import CliRunner
from pocket_cli.cli.main_cli import main


@pytest.mark.parametrize(
    "args",
    [
        ["static"],
        ["static", "publish", "--stage=prod"],
        ["static", "hash", "generate", "--stage", "dev"],
        ["static", "--help"],
    ],
)
def test_static_guides_to_pocket_rs(args):
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 2
    assert "pocket-rs static" in result.stderr
    assert "guide/cli/#pocket-rs" in result.stderr
    assert result.stdout == ""


def test_static_echoes_replacement_command():
    result = CliRunner().invoke(main, ["static", "publish", "--stage=prod"])
    assert "実行: pocket-rs static publish --stage=prod" in result.stderr
