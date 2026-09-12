"""共通 build と Django 別名の互換性・dirty ガードを確認する。"""

from unittest.mock import Mock

import pytest
from click.testing import CliRunner
from pocket_cli import django_cli
from pocket_cli.cli import build_cli
from pocket_cli.cli.main_cli import main


@pytest.mark.parametrize("command", [["build"], ["django", "build"]])
def test_build_checks_dirty_before_building(command, monkeypatch):
    monkeypatch.setattr(build_cli, "check_aws_credentials", lambda: None)
    monkeypatch.setattr(build_cli, "is_working_tree_dirty", lambda: True)
    monkeypatch.setattr(build_cli, "get_commit_hash", lambda: "abc123")
    monkeypatch.setattr(build_cli.Context, "from_toml", lambda **kwargs: "context")
    build = Mock(return_value=["repo:abc123"])
    monkeypatch.setattr(build_cli, "build_image", build)
    result = CliRunner().invoke(main, [*command, "--stage", "live"])
    assert result.exit_code != 0
    assert "未コミット" in result.output
    build.assert_not_called()
    result = CliRunner().invoke(main, [*command, "--stage", "live", "--allow-dirty"])
    assert result.exit_code == 0, result.output
    build.assert_called_once_with("context", tag="abc123")


def test_django_build_is_same_command():
    assert django_cli.django.commands["build"] is main.commands["build"]
