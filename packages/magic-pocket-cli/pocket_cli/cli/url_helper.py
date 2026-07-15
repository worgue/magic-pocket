from __future__ import annotations

from typing import Callable

import click

from pocket.context import Context
from pocket_cli.mediator import Mediator


def run_get_url(
    *,
    stage: str,
    secret_type: str,
    db_label: str,
    live_url: Callable[[Context], str],
    live: bool = False,
    live_rotates_credentials: bool = False,
) -> None:
    """DB 接続 URL を stdout に純テキストで出力する (`pocket resource <db> url`)。

    移行ツール等が ``$(pocket resource neon url --stage prod)`` で食える「URL 文字列
    だけ」を stdout に出す。診断・警告はすべて stderr に流し、stdout を汚さない。

    解決方式:

    - default (stored-first): ``type=<secret_type>`` の stored user secret を store から
      読む。副作用が無く、consumer (app) が実際に使う URL と一致する。未 provision なら
      provider API での live 算出に fallback する。
    - ``--live``: stored を見ず必ず provider API で live 算出する。常に最新だが、reveal
      API を持たない backend (TiDB 等) では **root password を rotate する** 点に注意
      (consumer の redeploy が前提)。

    ``live_rotates_credentials=True`` の backend では、default モードからの live
    fallback は破壊的 (credential rotate) なため確認プロンプトを挟む。``--live``
    明示時はオプションの help で rotate を告知済みなので確認しない。

    dual-declaration (移行中に ``[neon]`` と ``[tidb]`` を併記) 下でも、resource ごとに
    neon / tidb を呼び分ければ source/target 双方を解決できる。
    """
    context = Context.from_toml(stage=stage)

    if live:
        # --live は明示要求。失敗時は例外をそのまま伝播させる (fallback しない)。
        click.echo(live_url(context))
        return

    stored = _read_stored_url(context, secret_type)
    if stored is not None:
        click.echo(stored)
        return

    click.echo(
        "%s: stored user secret が未 provision のため provider API で live 算出します"
        " (reveal API の無い backend では root password が rotate されます)。"
        % db_label,
        err=True,
    )
    if live_rotates_credentials:
        click.confirm(
            "%s の live 算出は root password を rotate し、稼働中 consumer の"
            "接続を無効化します。続行しますか？" % db_label,
            abort=True,
            err=True,
        )
    try:
        url = live_url(context)
    except Exception as e:
        raise click.ClickException(
            "%s の接続 URL を解決できませんでした。stored secret が無く live 算出も"
            "失敗 (%s)。[<db>] 宣言 + provider 資格情報、または store-url を"
            "確認してください。" % (db_label, e)
        ) from None
    click.echo(url)


def _read_stored_url(context: Context, secret_type: str) -> str | None:
    """``type=<secret_type>`` の stored URL を読む。未 provision なら None。

    consumer の user secret 宣言が在れば ``spec.store`` override を尊重して読む。
    宣言が無くても (dual-declaration で DATABASE_URL が別 backend を指す等)、
    type 基準の正準パスを直接構築して読む — つまり「その backend の stored URL」を
    consumer 宣言の有無と無関係に引ける。両方無ければ None (→ live fallback)。
    """
    sc = context.awscontainer.secrets if context.awscontainer else None
    if sc is None:
        return None
    mediator = Mediator(context)
    specs = [spec for spec in sc.user.values() if spec.type == secret_type]
    if len(specs) > 1:
        # 通常は settings の check_user_type_unique で弾かれる (防御的)。
        raise click.ClickException(
            "type=%s の stored user secret が複数宣言されているため曖昧です。"
            % secret_type
        )
    if specs:
        return mediator.read_user_secret(specs[0])
    return mediator.read_stored_url_by_type(secret_type)
