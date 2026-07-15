from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pathspec

DEFAULT_EXCLUDES = [
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "*.pyc",
    ".env",
    ".forge",
]


def load_dockerignore(dockerfile_dir: Path) -> pathspec.PathSpec:
    """
    .dockerignore を読み込み、PathSpec を返す。
    ファイルがなければデフォルトの除外パターンを使う。

    pathspec の gitignore を使用。dockerignore は gitignore とほぼ同一の
    パターン仕様（`**`、末尾 `/`、否定 `!`、文字クラス等）を持つため、
    pathspec の gitignore で実用上問題なくカバーできる。
    """
    dockerignore_path = dockerfile_dir / ".dockerignore"
    if not dockerignore_path.exists():
        lines = list(DEFAULT_EXCLUDES)
    else:
        lines = [
            stripped
            for line in dockerignore_path.read_text().splitlines()
            if (stripped := line.strip()) and not stripped.startswith("#")
        ]
    return pathspec.PathSpec.from_lines("gitignore", lines)


def should_include(path: str, spec: pathspec.PathSpec) -> bool:
    """パスがパターンにマッチしないか（= 含めるべきか）を判定する。"""
    return not spec.match_file(path)


def has_negation(spec: pathspec.PathSpec) -> bool:
    """`!` (再包含) パターンを含むかどうか。"""
    return any(p.include is False for p in spec.patterns)


def iter_source_files(
    context_dir: Path, spec: pathspec.PathSpec
) -> Iterator[tuple[str, str]]:
    """dockerignore 適用済みの (絶対パス, 相対パス) を決定的順序で列挙する。

    dockerignore 仕様では `!` は除外ディレクトリ配下のファイルも再包含できる
    (docker CLI backend と同挙動にする必要がある)。ディレクトリ枝刈りをすると
    再包含対象へ到達できないため、否定パターンがある場合は枝刈りを無効化して
    ファイル単位で判定する。無い場合は従来どおり枝刈りして高速化する。
    """
    prune = not has_negation(spec)
    for dirpath, dirnames, filenames in os.walk(context_dir):
        rel_dir = os.path.relpath(dirpath, context_dir)
        if rel_dir == ".":
            rel_dir = ""
        if prune:
            # 除外ディレクトリを in-place で枝刈り (rglob より高速)
            dirnames[:] = sorted(
                d
                for d in dirnames
                if should_include(os.path.join(rel_dir, d) if rel_dir else d, spec)
            )
        else:
            dirnames.sort()
        for filename in sorted(filenames):
            rel = os.path.join(rel_dir, filename) if rel_dir else filename
            if should_include(rel, spec):
                yield os.path.join(dirpath, filename), rel
