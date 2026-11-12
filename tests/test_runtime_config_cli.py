"""`pocket runtime-config` (廃止予定) の生成物のテスト。"""

from __future__ import annotations

import pytest
import tomllib


def test_runtime_config_escapes_toml_strings():
    """文字列値の " と \\ がエスケープされ、生成 TOML がパース可能なこと

    以前は単純連結だったため、引用符やバックスラッシュを含む値 (schedules の
    manage コマンド等) で不正 TOML が image に焼き込まれ、Lambda INIT で
    初めて落ちていた。
    """
    from pocket_cli.cli.runtime_config_cli import _to_toml

    tricky = 'say "hello" C:\\path\\to'
    toml_str = _to_toml({"general": {"note": tricky, "items": ['a"b']}})
    parsed = tomllib.loads(toml_str)
    assert parsed["general"]["note"] == tricky
    assert parsed["general"]["items"] == ['a"b']


def test_runtime_config_stdout_is_toml_and_warns_deprecation(monkeypatch):
    """stdout には TOML だけを出し、廃止予定の案内は stderr に出す。"""
    from pathlib import Path

    from click.testing import CliRunner
    from pocket_cli.cli.runtime_config_cli import runtime_config

    monkeypatch.setattr(
        "pocket_cli.cli.runtime_config_cli.get_toml_path",
        lambda: Path("tests/data/toml/default.toml"),
    )
    result = CliRunner().invoke(runtime_config, ["-"])
    assert result.exit_code == 0, result.output
    assert "general" in tomllib.loads(result.stdout)
    assert "廃止予定" not in result.stdout
    assert "廃止予定" in result.stderr


def test_runtime_config_quotes_keys_with_special_chars():
    """bare key に使えない文字を含むキーが quote されること

    IAM Condition の `"kms:ViaService"` のように ":" を含むキーを bare のまま
    書くと不正 TOML になり、image に焼き込まれて Lambda INIT で全 handler が
    TOMLDecodeError で死ぬ (値側の escape と同型の事故がキー側で起きていた)。
    """
    from pocket_cli.cli.runtime_config_cli import _to_toml

    data = {
        "general": {"kms:ViaService": "ssm.ap-northeast-1.amazonaws.com"},
        "policy": {"email-login.gate": {"kms:Decrypt": True}},
    }
    parsed = tomllib.loads(_to_toml(data))
    assert parsed["general"]["kms:ViaService"] == "ssm.ap-northeast-1.amazonaws.com"
    assert parsed["policy"]["email-login.gate"] == {"kms:Decrypt": True}


def test_runtime_config_renders_nested_inline_tables():
    """inline table の中のネスト dict / list が TOML として出ること

    以前は repr() だったため Python 表記 ({'StringLike': {...}}) が出力され、
    IAM Statement の Condition で不正 TOML になっていた。
    """
    from pocket_cli.cli.runtime_config_cli import _to_toml

    statement = {
        "Effect": "Allow",
        "Action": ["kms:Decrypt"],
        "Resource": "*",
        "Condition": {"StringLike": {"kms:ViaService": "ssm.*.amazonaws.com"}},
    }
    parsed = tomllib.loads(_to_toml({"policy": {"Statement": [statement]}}))
    assert parsed["policy"]["Statement"] == [statement]


def test_runtime_config_self_check_rejects_invalid_toml(tmp_path, monkeypatch):
    """生成物が不正 TOML なら生成時点で落ちること (INIT まで遅延させない)"""
    from pocket_cli.cli import runtime_config_cli

    src = tmp_path / "pocket.toml"
    src.write_text('[general]\nproject_name = "x"\n')
    monkeypatch.setattr(runtime_config_cli, "get_toml_path", lambda: src)
    # dumper が壊れた体を作り、self-check が捕まえることを確認する
    monkeypatch.setattr(runtime_config_cli, "_to_toml", lambda data: "broken = ")

    with pytest.raises(runtime_config_cli.RuntimeConfigGenerationError):
        runtime_config_cli._runtime_config_str()


def test_runtime_config_drops_container_iam(tmp_path, monkeypatch):
    """container.<name>.iam は provision 専用なので runtime toml に載らないこと"""
    from pocket_cli.cli import runtime_config_cli

    src = tmp_path / "pocket.toml"
    src.write_text(
        "[general]\n"
        'project_name = "x"\n'
        'region = "us"\n'
        'stages = ["dev"]\n'
        "\n"
        "[container.web]\n"
        'dockerfile_path = "Dockerfile"\n'
        "\n"
        "[container.web.iam]\n"
        'managed_policy_arns = ["arn:aws:iam::aws:policy/ReadOnlyAccess"]\n'
    )
    monkeypatch.setattr(runtime_config_cli, "get_toml_path", lambda: src)

    parsed = tomllib.loads(runtime_config_cli._runtime_config_str())
    assert "iam" not in parsed["container"]["web"]
