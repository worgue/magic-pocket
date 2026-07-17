"""CLI の resource 取得で共通に使う設定ゲート。

`Context.from_toml` で読んだ context のセクションが未設定のとき、系統ごとに
バラバラだった例外 (Exception / ValueError / 生 raise) を click.ClickException に
統一して投げる。ClickException は PocketCLI (ValueError しか整形しない) ではなく
click 本体が整形するため、どの系統でも生 traceback ではなく "Error: ..." が出る。

なお `Context.from_toml` 自体が投げる AWS 由来の例外 (認証エラー等) はここでは
触らない。ゲート対象は「設定が無い」ケースだけで、AWS エラーは従来どおり伝播させる。
"""

from __future__ import annotations

from typing import TypeVar

import click

T = TypeVar("T")


def require_configured(value: T | None, message: str) -> T:
    """設定セクションが未設定 (falsy) なら ClickException を投げ、あれば値を返す。"""
    if not value:
        raise click.ClickException(message)
    return value
