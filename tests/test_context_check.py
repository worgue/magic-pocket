"""build context のファイル permission チェックのテスト。

mode 600 のファイルが image に COPY され、Lambda の非 root 実行ユーザーが
読めず全 handler が INIT 失敗 (サイトごと 500) した実害への回帰テスト
(2026-07-20 起票の feedback)。
"""

from pathlib import Path

import pytest
from pocket_cli.resources.aws.builders import context_check
from pocket_cli.resources.aws.builders.context_check import (
    find_files_without_world_read,
    resummarize_world_read_warnings,
    warn_files_without_world_read,
    warned_files_with_mode,
)


@pytest.fixture(autouse=True)
def _reset_warned_files():
    context_check._warned_files.clear()
    yield
    context_check._warned_files.clear()


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


def test_warn_shows_mode(tmp_path, capsys):
    """警告にはファイル名だけでなく mode も出す (2026-08-26 受領 feedback)"""
    _context(tmp_path)
    (tmp_path / "pyproject.toml").chmod(0o600)
    warn_files_without_world_read(tmp_path)
    assert "pyproject.toml (600)" in capsys.readouterr().err.replace("\n", "")


def test_resummarize_repeats_warning_at_deploy_end(tmp_path, capsys):
    """build 中の警告はログに埋もれるため deploy 終了時に再掲する

    (2026-08-26 受領 feedback: 警告に気付けず Lambda の INIT 失敗まで到達し、
    版不整合の方を先に疑って遠回りした実害への回帰テスト)
    """
    _context(tmp_path)
    (tmp_path / "pyproject.toml").chmod(0o600)
    warn_files_without_world_read(tmp_path)
    # multi-container で同じ context を複数回 build しても重複しては積まない
    warn_files_without_world_read(tmp_path)
    assert warned_files_with_mode() == ["pyproject.toml (600)"]
    capsys.readouterr()
    resummarize_world_read_warnings()
    err = capsys.readouterr().err.replace("\n", "")
    assert "pyproject.toml (600)" in err
    assert "chmod 644" in err


def test_resummarize_silent_when_clean(tmp_path, capsys):
    _context(tmp_path)
    warn_files_without_world_read(tmp_path)
    resummarize_world_read_warnings()
    assert capsys.readouterr().err == ""


def test_warn_silent_when_clean(tmp_path, capsys):
    _context(tmp_path)
    assert warn_files_without_world_read(tmp_path) == []
    assert capsys.readouterr().err == ""


def test_init_critical_pocket_toml_raises(tmp_path):
    """pocket.toml は runtime が INIT で読むため警告でなくエラーに昇格する

    (2026-08-19 受領 feedback の運用知見: mode 600 の pocket.toml で Rust
    container が INIT PermissionDenied。警告では deploy が止まらない)
    """
    _context(tmp_path)
    (tmp_path / "pocket.toml").write_text("[general]")
    (tmp_path / "pocket.toml").chmod(0o600)
    with pytest.raises(RuntimeError, match="pocket.toml"):
        warn_files_without_world_read(tmp_path)


def test_init_critical_runtime_toml_in_subdir_raises(tmp_path):
    """pocket.runtime.toml は配置先 (django project_dir 等) を問わずエラー"""
    _context(tmp_path)
    sub = tmp_path / "mysite"
    sub.mkdir()
    (sub / "pocket.runtime.toml").write_text("[general]")
    (sub / "pocket.runtime.toml").chmod(0o600)
    with pytest.raises(RuntimeError, match="pocket.runtime.toml"):
        warn_files_without_world_read(tmp_path)


def test_non_critical_files_stay_warning(tmp_path, capsys):
    """COPY --chmod で正規化する構成があるため一般ファイルは警告のまま"""
    _context(tmp_path)
    (tmp_path / "pocket.toml").write_text("[general]")
    (tmp_path / "pyproject.toml").chmod(0o600)
    assert warn_files_without_world_read(tmp_path) == ["pyproject.toml"]
    assert "chmod 644" in capsys.readouterr().err.replace("\n", "")


def test_generate_runtime_config_forces_world_read(tmp_path, monkeypatch):
    """生成した pocket.runtime.toml は strict umask 下でも other-read を持つ"""
    import os

    from pocket_cli.cli.runtime_config_cli import generate_runtime_config

    monkeypatch.chdir(tmp_path)
    (tmp_path / "pocket.toml").write_text(
        '[general]\nproject_name = "x"\nstages = ["dev"]\nregion = "ap-northeast-1"\n'
    )
    old_umask = os.umask(0o077)
    try:
        out = tmp_path / "pocket.runtime.toml"
        generate_runtime_config(out)
    finally:
        os.umask(old_umask)
    assert out.stat().st_mode & 0o044 == 0o044
