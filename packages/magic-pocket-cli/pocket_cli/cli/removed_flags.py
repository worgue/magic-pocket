"""廃止済み CLI フラグを移行案内つきで fail-fast させる click オプション。

click は未知のオプションを "Error: No such option" とだけ報告し、移行先が
分からないまま CI が停止する。設定ファイル側の廃止キーは settings の
model_validator が移行手順つきで fail-fast するのに対し、CLI フラグ側にも
同等のガイドを出すため、廃止フラグを hidden オプションとして残し、使われたら
移行手順を示して即エラーにする。
"""

from __future__ import annotations

import click


def _reject_skip_check_existing(ctx, param, value):
    if value:
        raise click.UsageError(
            "--skip-check-existing は 0.6.0 で廃止されました (credential-less "
            'deploy は provisioning = "command" に一本化)。pocket.toml の '
            '[<neon|tidb|upstash>] に `provisioning = "command"` を設定し、'
            "[container.<name>.secrets.user] に接続 URL を type で宣言 "
            '(例: `DATABASE_URL = { type = "neon_database_url" }`) したうえで、'
            "deploy 前に `pocket resource <db> store-url --stage <stage>` を"
            "一度実行してください。"
        )


# deploy / promote 系コマンドに付ける。expose_value=False なのでコマンド関数の
# シグネチャには現れない (使われた場合のみ callback がエラーを投げる)。
removed_skip_check_existing = click.option(
    "--skip-check-existing",
    is_flag=True,
    default=False,
    hidden=True,
    expose_value=False,
    callback=_reject_skip_check_existing,
    help="(0.6.0 で廃止)",
)
