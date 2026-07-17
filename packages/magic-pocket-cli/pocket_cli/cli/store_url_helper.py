from __future__ import annotations

from typing import Callable

import click

from pocket.context import Context
from pocket.utils import echo


def run_store_url(
    *,
    stage: str,
    secret_type: str,
    db_label: str,
    key: str | None,
    force: bool,
    ensure_and_compute_url: Callable[[Context], str],
) -> None:
    """`pocket <db> store-url` の共通処理。

    対象 stored user secret を特定 → (既存かつ非 force なら no-op) → リソースを
    ensure し URL を算出 → 正準名へ書き込む。`ensure_and_compute_url` は DB ごとの
    ensure + URL 算出。
    """
    context = Context.from_toml(stage=stage)
    sc = context.awscontainer.secrets if context.awscontainer else None
    if sc is None:
        raise click.ClickException(
            "awscontainer.secrets が設定されていません。"
            "[awscontainer.secrets.user] に DATABASE_URL を宣言してください。"
        )

    if key is not None:
        spec = sc.user.get(key)
        if spec is None:
            raise click.ClickException("secrets.user に '%s' がありません。" % key)
        if spec.type != secret_type:
            raise click.ClickException(
                "secrets.user '%s' は type=%s ではありません (type=%s)。"
                % (key, secret_type, spec.type)
            )
        target_key = key
    else:
        candidates = [k for k, spec in sc.user.items() if spec.type == secret_type]
        if not candidates:
            raise click.ClickException(
                "type=%s の stored user secret が宣言されていません。"
                "[awscontainer.secrets.user] に "
                '`DATABASE_URL = { type = "%s" }` を追加してください。'
                % (secret_type, secret_type)
            )
        if len(candidates) > 1:
            raise click.ClickException(
                "type=%s の user secret が複数あります (%s)。--key で指定してください。"
                % (secret_type, ", ".join(candidates))
            )
        target_key = candidates[0]

    spec = sc.user[target_key]

    if not force and sc.user_store.exists(spec):
        echo.warning(
            "%s は既に存在します (%s)。rotate する場合は --force を付けてください。"
            % (target_key, spec.name)
        )
        return

    url = ensure_and_compute_url(context)
    sc.user_store.put(spec, url)
    echo.success(
        "%s URL を %s (%s) に保存しました。" % (db_label, target_key, spec.name)
    )
