"""build context のファイル permission チェックのテスト。

mode 600 のファイルが image に COPY され、Lambda の非 root 実行ユーザーが
読めず全 handler が INIT 失敗 (サイトごと 500) した実害への回帰テスト
(2026-07-20 起票の feedback)。
"""

from pathlib import Path

from pocket_cli.resources.aws.builders.context_check import (
    find_files_without_world_read,
    warn_files_without_world_read,
)


def _context(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("x")
    (tmp_path / "pyproject.toml").write_text("[project]")
    return tmp_path


def test_detects_file_without_world_read(tmp_path):
    _context(tmp_path)
    (tmp_path / "pyproject.toml").chmod(0o600)
    assert find_files_without_world_read(tmp_path) == ["pyproject.toml"]


def test_all_world_readable_returns_empty(tmp_path):
    _context(tmp_path)
    assert find_files_without_world_read(tmp_path) == []


def test_dockerignored_files_are_not_checked(tmp_path):
    """dockerignore で除外されるファイルは image に入らないので検査しない"""
    _context(tmp_path)
    (tmp_path / ".dockerignore").write_text("secret.key\n")
    (tmp_path / "secret.key").write_text("k")
    (tmp_path / "secret.key").chmod(0o600)
    assert find_files_without_world_read(tmp_path) == []


def test_broken_symlink_is_skipped(tmp_path):
    """壊れた symlink (実体なし) は stat せず skip する (zip 側と同様)"""
    _context(tmp_path)
    (tmp_path / "dangling").symlink_to(tmp_path / "no-such-file")
    assert find_files_without_world_read(tmp_path) == []


def test_warn_lists_files_and_guidance(tmp_path, capsys):
    _context(tmp_path)
    (tmp_path / "pyproject.toml").chmod(0o600)
    detected = warn_files_without_world_read(tmp_path)
    assert detected == ["pyproject.toml"]
    err = capsys.readouterr().err.replace("\n", "")
    assert "pyproject.toml" in err
    assert "chmod 644" in err


def test_warn_silent_when_clean(tmp_path, capsys):
    _context(tmp_path)
    assert warn_files_without_world_read(tmp_path) == []
    assert capsys.readouterr().err == ""
