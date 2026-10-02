"""pocket の Django system check。

`get_storages()` が登録する。`collectstatic` / `runserver` / `check` で実行され、
`pocket django deploy` は Lambda の更新前に collectstatic を走らせるため、
deploy 後に問題が表に出る前にここで止まる。
"""

import os
import urllib.parse

from django.conf import settings
from django.core import checks

# deploy_hash route を使う staticfiles の route prefix (`/static/`)。
# get_storages() が複数回呼ばれても check の登録は 1 回で済むよう、関数ではなく
# この値を差し替える
_deploy_hash_static: dict[str, str] = {}


def register_deploy_hash_static_check(route_prefix: str) -> None:
    _deploy_hash_static["prefix"] = route_prefix
    checks.register(check_deploy_hash_static_url, checks.Tags.staticfiles)


def check_deploy_hash_static_url(app_configs=None, **kwargs) -> list[checks.Error]:
    """STATIC_URL が `<route の prefix>{DEPLOY_HASH}/` の形かを検査する。

    CloudFront Function は route の prefix 直下の 1 セグメントだけを hash として
    外す (KN1670)。hash が別の位置にあると外れず、static がすべて 403 になる。
    DEPLOY_HASH が未設定 (ローカル開発で `dev` 等に落ちる) なら検査しない。
    """
    route_prefix = _deploy_hash_static.get("prefix")
    deploy_hash = os.environ.get("DEPLOY_HASH")
    if not route_prefix or not deploy_hash:
        return []
    message = deploy_hash_static_url_error(
        settings.STATIC_URL, route_prefix, deploy_hash
    )
    if message is None:
        return []
    return [checks.Error(message, id="pocket.E001")]


def deploy_hash_static_url_error(
    static_url: str | None, route_prefix: str, deploy_hash: str
) -> str | None:
    """STATIC_URL の形が合わなければ原因と正しい形を示すメッセージを返す。"""
    prefix = "/" + route_prefix.strip("/") + "/"
    expected = f"{prefix}{deploy_hash}/"
    path = urllib.parse.urlsplit(static_url or "").path
    if not path.startswith("/"):
        path = "/" + path
    if path == expected:
        return None
    setting = prefix.lstrip("/") + "{DEPLOY_HASH}/"
    return (
        f"STATIC_URL '{static_url}' が deploy_hash route と合いません "
        f"(期待する形: '{expected}')。CloudFront Function は route の prefix "
        f"'{prefix}' の直後のセグメントだけを hash として外すため、このままでは "
        "static が 403 になるか (hash が外れない)、hash を含まない URL が"
        "長期キャッシュされて deploy 後も古いファイルが返ります。settings.py で "
        f'STATIC_URL = f"{setting}" としてください。'
    )
