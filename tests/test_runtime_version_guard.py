"""CLI/runtime バージョン不整合ガード (層2) のテスト。

検証対象:
- pocket.utils.parse_generator_version / version_tuple
- Settings.check_generator_version: generator > runtime で raise、それ以外は no-op
- generate_runtime_config: 生成元版をマーカーコメントで刻む (旧 runtime が無視できる形)
"""

from __future__ import annotations

import pytest
import tomllib

from pocket import settings
from pocket.utils import (
    GENERATOR_VERSION_MARKER,
    parse_generator_version,
    version_tuple,
)


def test_version_tuple_parses_and_orders_numerically():
    assert version_tuple("0.10.0") == (0, 10, 0)
    assert version_tuple("0.7.0") == (0, 7, 0)
    # 辞書順ではなく数値順 (10 > 7)
    assert version_tuple("0.10.0") > version_tuple("0.7.0")
    # pre-release suffix は先頭数字だけ採る
    assert version_tuple("1.2.3rc1") == (1, 2, 3)


def test_parse_generator_version_from_marker():
    text = "%s 0.10.0\n[general]\nx = 1\n" % GENERATOR_VERSION_MARKER
    assert parse_generator_version(text) == "0.10.0"


def test_parse_generator_version_absent_returns_none():
    assert parse_generator_version("[general]\nx = 1\n") is None


def test_check_generator_version_raises_when_generator_newer(monkeypatch):
    monkeypatch.setattr("pocket.__version__", "0.7.0", raising=False)
    text = "%s 0.10.0\n[general]\n" % GENERATOR_VERSION_MARKER
    with pytest.raises(ValueError, match=r"magic-pocket\[django\]>=0.10.0"):
        settings.Settings.check_generator_version(text)


def test_check_generator_version_noop_when_equal(monkeypatch):
    monkeypatch.setattr("pocket.__version__", "0.10.0", raising=False)
    text = "%s 0.10.0\n[general]\n" % GENERATOR_VERSION_MARKER
    settings.Settings.check_generator_version(text)  # 例外が出なければ OK


def test_check_generator_version_noop_when_runtime_newer(monkeypatch):
    monkeypatch.setattr("pocket.__version__", "0.11.0", raising=False)
    text = "%s 0.10.0\n[general]\n" % GENERATOR_VERSION_MARKER
    settings.Settings.check_generator_version(text)


def test_check_generator_version_noop_when_marker_absent():
    settings.Settings.check_generator_version("[general]\n")  # マーカー無し → no-op


def test_generate_runtime_config_stamps_marker_and_stays_valid_toml(
    tmp_path, monkeypatch
):
    from pocket_cli.cli import runtime_config_cli

    src = tmp_path / "pocket.toml"
    src.write_text('[general]\nproject_name = "x"\nregion = "us"\nstages = ["dev"]\n')
    out = tmp_path / "pocket.runtime.toml"
    monkeypatch.setattr(runtime_config_cli, "get_toml_path", lambda: src)
    monkeypatch.setattr(runtime_config_cli, "_generator_version", lambda: "0.10.0")

    runtime_config_cli.generate_runtime_config(out)

    text = out.read_text()
    # 先頭にマーカーコメントが刻まれる
    assert text.startswith("%s 0.10.0" % GENERATOR_VERSION_MARKER)
    assert parse_generator_version(text) == "0.10.0"
    # コメントなので tomllib は普通に読める (旧 runtime の後方互換)
    parsed = tomllib.loads(text)
    assert parsed["general"]["project_name"] == "x"
    assert "_generator_version" not in parsed  # トップレベルキーとしては入れない
