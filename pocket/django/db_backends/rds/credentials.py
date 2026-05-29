from __future__ import annotations

import os
import urllib.parse
from collections.abc import Callable
from typing import Any

# PostgreSQL SQLSTATE Class 28 = Invalid Authorization Specification
# (28P01 = invalid_password, 28000 = invalid_authorization_specification)。
# master password ローテーション後の旧パスワード接続はこのクラスで弾かれる。
_AUTH_SQLSTATE_PREFIX = "28"
_AUTH_MESSAGE_HINTS = (
    "password authentication failed",
    "authentication failed",
)


def is_auth_error(exc: BaseException | None) -> bool:
    """例外チェーンを辿り PostgreSQL の認証失敗かどうかを判定する。

    psycopg を import せずに判定できるよう、SQLSTATE (psycopg は ``sqlstate``、
    psycopg2 は ``pgcode``) とメッセージ文字列の両方を見る。Django は psycopg
    エラーを ``django.db.utils.OperationalError`` でラップするため、``__cause__`` /
    ``__context__`` も辿る。
    """
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        code = getattr(e, "sqlstate", None) or getattr(e, "pgcode", None)
        if code and str(code).startswith(_AUTH_SQLSTATE_PREFIX):
            return True
        if any(hint in str(e).lower() for hint in _AUTH_MESSAGE_HINTS):
            return True
        stack.append(e.__cause__)
        stack.append(e.__context__)
    return False


def refresh_rds_settings(settings_dict: dict[str, Any]) -> bool:
    """RDS シークレットを再取得し、settings_dict の認証情報を最新値で更新する。

    RDS 認証情報 (Secrets Manager の ``POCKET_RDS_SECRET_ARN`` または SSM の
    ``POCKET_RDS_SSM_PARAM``) から ``DATABASE_URL`` を再構築し、その値を parse して
    settings_dict を上書きする。これにより以降の再接続は新パスワードで直接成功する。
    更新できれば True、RDS 認証情報が無い (= RDS 以外) 場合は False。
    """
    if not os.environ.get("POCKET_RDS_SECRET_ARN") and not os.environ.get(
        "POCKET_RDS_SSM_PARAM"
    ):
        return False
    from pocket.runtime import _set_rds_database_url

    _set_rds_database_url()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return False
    parsed = urllib.parse.urlparse(database_url)
    settings_dict["USER"] = urllib.parse.unquote(parsed.username or "")
    settings_dict["PASSWORD"] = urllib.parse.unquote(parsed.password or "")
    settings_dict["HOST"] = parsed.hostname or ""
    settings_dict["PORT"] = str(parsed.port or "")
    settings_dict["NAME"] = parsed.path.lstrip("/")
    return True


def connect_with_credential_refresh(
    connect: Callable[[dict[str, Any]], Any],
    conn_params: dict[str, Any],
    settings_dict: dict[str, Any],
    build_params: Callable[[], dict[str, Any]],
) -> Any:
    """``connect(conn_params)`` を試み、認証失敗時のみ secret を再取得して再接続する。

    通常 (認証成功 / 認証以外のエラー) は素通しなので warm 接続にオーバーヘッドは無い。
    認証失敗時だけ ``refresh_rds_settings`` で settings_dict を更新し、
    ``build_params()`` で最新の接続パラメータを作り直して 1 度だけ再接続する。
    """
    try:
        return connect(conn_params)
    except Exception as exc:
        if not is_auth_error(exc):
            raise
        if not refresh_rds_settings(settings_dict):
            raise
        return connect(build_params())
