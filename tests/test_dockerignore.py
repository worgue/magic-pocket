from pathlib import Path

from pocket_cli.resources.aws.builders.dockerignore import (
    load_dockerignore,
    should_include,
)


def _write(tmp_path: Path, content: str) -> Path:
    (tmp_path / ".dockerignore").write_text(content)
    return tmp_path


def test_trailing_slash_directory_pattern(tmp_path):
    """末尾スラッシュ付きパターンがディレクトリ全体を除外すること。"""
    _write(tmp_path, "node_modules/\n.venv/\ndata/static_image/\n")
    spec = load_dockerignore(tmp_path)
    assert should_include("src/main.py", spec)
    assert not should_include("node_modules/foo/bar.js", spec)
    assert not should_include(".venv/lib/python3.12/site.py", spec)
    assert not should_include("data/static_image/large.png", spec)


def test_no_trailing_slash_equivalent(tmp_path):
    """末尾スラッシュあり / なしがどちらも機能すること。"""
    _write(tmp_path, "node_modules\n")
    spec = load_dockerignore(tmp_path)
    assert not should_include("node_modules/foo/bar.js", spec)


def test_negate_pattern(tmp_path):
    """否定パターンで再包含できること。"""
    _write(tmp_path, "data/\n!data/keep/\n")
    spec = load_dockerignore(tmp_path)
    assert not should_include("data/large.bin", spec)
    assert should_include("data/keep/important.txt", spec)


def test_glob_double_star(tmp_path):
    """`**` パターンが再帰的に動作すること。"""
    _write(tmp_path, "**/*.pyc\n")
    spec = load_dockerignore(tmp_path)
    assert not should_include("foo/bar/baz.pyc", spec)
    assert should_include("foo/bar/baz.py", spec)


def test_comment_and_blank_lines(tmp_path):
    _write(
        tmp_path,
        """
# this is a comment
node_modules/

.venv/
""",
    )
    spec = load_dockerignore(tmp_path)
    assert not should_include("node_modules/foo.js", spec)
    assert not should_include(".venv/lib/x.py", spec)
    assert should_include("src/main.py", spec)


def test_default_excludes_when_no_dockerignore(tmp_path):
    """`.dockerignore` が無い場合はデフォルト除外が適用されること。"""
    spec = load_dockerignore(tmp_path)
    assert not should_include(".git/HEAD", spec)
    assert not should_include("node_modules/foo.js", spec)
    assert not should_include("__pycache__/foo.pyc", spec)
    assert should_include("src/main.py", spec)
