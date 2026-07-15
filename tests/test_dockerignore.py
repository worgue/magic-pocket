from pathlib import Path

from pocket_cli.resources.aws.builders.dockerignore import (
    iter_source_files,
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


def test_os_walk_prunes_excluded_directories(tmp_path):
    """os.walk での枝刈りが正しく動作すること (rglob の代替検証)"""
    import os

    # 構造: src/main.py, node_modules/pkg/index.js, .git/HEAD
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print()")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref")
    _write(tmp_path, "node_modules\n.git\n")
    spec = load_dockerignore(tmp_path)

    walked_dirs: list[str] = []
    included_files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(tmp_path):
        rel_dir = os.path.relpath(dirpath, tmp_path)
        if rel_dir == ".":
            rel_dir = ""
        dirnames[:] = [
            d
            for d in dirnames
            if should_include(os.path.join(rel_dir, d) if rel_dir else d, spec)
        ]
        walked_dirs.append(rel_dir)
        for f in filenames:
            rel = os.path.join(rel_dir, f) if rel_dir else f
            if should_include(rel, spec):
                included_files.append(rel)
    # node_modules と .git は walk されていないこと
    assert "node_modules" not in walked_dirs
    assert ".git" not in walked_dirs
    assert "src" in walked_dirs
    assert "src/main.py" in included_files
    assert all("node_modules" not in f for f in included_files)
    assert all(".git" not in f for f in included_files)


def test_default_excludes_when_no_dockerignore(tmp_path):
    """`.dockerignore` が無い場合はデフォルト除外が適用されること。"""
    spec = load_dockerignore(tmp_path)
    assert not should_include(".git/HEAD", spec)
    assert not should_include("node_modules/foo.js", spec)
    assert not should_include("__pycache__/foo.pyc", spec)
    assert should_include("src/main.py", spec)


def test_negation_reincludes_file_under_excluded_directory(tmp_path):
    """`!` は除外ディレクトリ配下のファイルも再包含できること (docker CLI と同仕様)

    以前は os.walk の枝刈りで除外ディレクトリへ到達できず、
    node_modules + !node_modules/keep.js の keep.js が source zip から漏れていた。
    """
    (tmp_path / ".dockerignore").write_text("node_modules\n!node_modules/keep.js\n")
    (tmp_path / "app.py").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "keep.js").write_text("k")
    (tmp_path / "node_modules" / "skip.js").write_text("s")

    spec = load_dockerignore(tmp_path)
    rels = {rel for _abs, rel in iter_source_files(tmp_path, spec)}
    assert "app.py" in rels
    assert "node_modules/keep.js" in rels
    assert "node_modules/skip.js" not in rels


def test_iter_source_files_prunes_without_negation(tmp_path):
    """否定パターンが無い場合は従来どおり除外ディレクトリが列挙されないこと"""
    (tmp_path / ".dockerignore").write_text("node_modules\n")
    (tmp_path / "app.py").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "skip.js").write_text("s")

    spec = load_dockerignore(tmp_path)
    rels = {rel for _abs, rel in iter_source_files(tmp_path, spec)}
    assert rels == {"app.py", ".dockerignore"}
