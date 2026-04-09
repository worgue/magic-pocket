from __future__ import annotations

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
