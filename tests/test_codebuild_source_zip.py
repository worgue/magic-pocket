"""CodeBuild builder の source zip 作成 (`_add_source_file`) の回帰テスト。

forge VM 由来の 2 footgun を吸収することを確認する:

- host 側参照などで壊れた symlink を zip に入れると ``FileNotFoundError`` で
  source zip 作成ごと落ち deploy 全体が失敗する → skip + warning する
- 0600 など他ユーザーが読めない mode のファイルがそのまま image に入ると
  Lambda の非 root 実行ユーザーが読めず起動時 panic する → 0644/0755 に正規化する
"""

from __future__ import annotations

import io
import os
import zipfile

import pytest
from pocket_cli.resources.aws.builders.codebuild import CodeBuildBuilder


@pytest.fixture
def builder() -> CodeBuildBuilder:
    # __init__ は boto3 client を作るだけ (AWS 通信なし)
    return CodeBuildBuilder(
        region="us-east-1",
        resource_prefix="test-",
        state_bucket="test-bucket",
    )


def _zip_and_read(
    builder: CodeBuildBuilder, files: list[tuple[str, str]]
) -> zipfile.ZipFile:
    """(full_path, arcname) を順に追加した zip を ZipFile として返す。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for full_path, arcname in files:
            builder._add_source_file(zf, full_path, arcname)
    buf.seek(0)
    return zipfile.ZipFile(buf, "r")


def _mode(zi: zipfile.ZipInfo) -> int:
    return (zi.external_attr >> 16) & 0o777


def test_broken_symlink_is_skipped(builder, tmp_path, capsys):
    target = tmp_path / "real.txt"
    target.write_text("hi")
    good = tmp_path / "good.txt"
    good.write_text("keep me")
    broken = tmp_path / "broken.txt"
    broken.symlink_to(tmp_path / "does-not-exist")

    zf = _zip_and_read(
        builder,
        [
            (str(good), "good.txt"),
            (str(broken), "broken.txt"),
        ],
    )
    names = zf.namelist()
    assert "good.txt" in names
    assert "broken.txt" not in names  # 壊れた symlink は skip
    # warning は stderr (echo.warning) に出る
    assert "broken.txt" in capsys.readouterr().err


def test_mode_0600_is_normalized_to_0644(builder, tmp_path):
    f = tmp_path / "secret.toml"
    f.write_text("x = 1")
    os.chmod(f, 0o600)

    zf = _zip_and_read(builder, [(str(f), "secret.toml")])
    assert _mode(zf.getinfo("secret.toml")) == 0o644


def test_executable_mode_is_normalized_to_0755(builder, tmp_path):
    f = tmp_path / "entrypoint.sh"
    f.write_text("#!/bin/sh\n")
    # 意図的な test 入力: builder が実行ファイルの 0700 を 0755 に正規化することを
    # 検証する。semgrep insecure-file-permissions は owner 実行ビット付き chmod を
    # 機械的に flag するため、この行だけ抑制する (テストデータであり実コードではない)。
    os.chmod(f, 0o700)  # nosemgrep

    zf = _zip_and_read(builder, [(str(f), "entrypoint.sh")])
    assert _mode(zf.getinfo("entrypoint.sh")) == 0o755


def test_content_is_preserved(builder, tmp_path):
    f = tmp_path / "app.py"
    f.write_text("print('hello')\n")
    os.chmod(f, 0o600)

    zf = _zip_and_read(builder, [(str(f), "app.py")])
    assert zf.read("app.py") == b"print('hello')\n"
