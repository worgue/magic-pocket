from __future__ import annotations

import fnmatch
from pathlib import Path

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


def parse_dockerignore(dockerfile_dir: Path) -> list[str]:
    """
    .dockerignore を読み込み、パターンリストを返す。
    ファイルがなければデフォルトの除外パターンを返す。
    """
    dockerignore_path = dockerfile_dir / ".dockerignore"
    if not dockerignore_path.exists():
        return list(DEFAULT_EXCLUDES)

    patterns: list[str] = []
    for line in dockerignore_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def should_include(path: str, patterns: list[str]) -> bool:
    """
    パスがパターンにマッチするか判定。
    '!' で始まるパターンは除外の例外（再包含）。
    最後にマッチしたパターンが採用される。
    """
    included = True
    for pattern in patterns:
        negate = pattern.startswith("!")
        if negate:
            pattern = pattern[1:]

        # ディレクトリ名の部分一致
        parts = path.split("/")
        matched = False
        if "/" not in pattern:
            # パターンにスラッシュがなければ各パス要素と照合
            for part in parts:
                if fnmatch.fnmatch(part, pattern):
                    matched = True
                    break
        else:
            # スラッシュがあればパス全体と照合
            matched = fnmatch.fnmatch(path, pattern)

        if matched:
            included = negate

    return included
