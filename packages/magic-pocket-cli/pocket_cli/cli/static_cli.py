"""`pocket static ...`: Rust 版 CLI (`pocket static`) への移行案内だけを出す stub。

静的系 (object_hash route の publish / adopt / purge / status / hash generate) は
Rust 版に一本化した。サブコマンド・オプションは何を渡しても同じ案内を出して
終了コード 2。
"""

from __future__ import annotations

import shlex
import sys

import click

INSTALL_DOCS_URL = "https://worgue.github.io/magic-pocket/guide/cli/#pocket-rs"


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    add_help_option=False,
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def static(args):
    """(移行済み) Rust 版 `pocket static` を使ってください。"""
    replacement = shlex.join(("pocket", "static", *args))
    click.echo(
        "`pocket static` は Rust 版 CLI に移りました。"
        "Python 版 (magic-pocket-cli) を外し、Rust 版の `pocket` を入れてください。\n"
        "  実行: %s\n"
        "  導入: %s" % (replacement, INSTALL_DOCS_URL),
        err=True,
    )
    sys.exit(2)
