"""pocket.toml の相対パスの基準 (project dir)。

pocket.toml は cwd から上方向に探すので、下位ディレクトリ (例: `frontend/`) から
実行しても設定は読める。pocket.toml に書く相対パス (`dockerfile_path` / route の
`build` / `upload_dir` / `managed_assets` / django の `project_dir`) とビルド
コンテキストも、cwd ではなく見つけた pocket.toml のディレクトリを基準にする
(Rust 版 `pocket-rs static` と同じ基準)。
"""

from __future__ import annotations

from pathlib import Path

from pocket.utils import get_toml_path


def project_dir() -> Path:
    """pocket.toml のあるディレクトリ (絶対パス)"""
    return get_toml_path().resolve().parent


def project_path(path: str) -> Path:
    """pocket.toml からの相対パスを絶対パスにする (絶対パスはそのまま)"""
    return project_dir() / path
