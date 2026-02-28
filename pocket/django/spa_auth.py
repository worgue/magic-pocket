from __future__ import annotations

import hashlib
import hmac
import os
import time

COOKIE_NAME = "pocket-spa-token"
DEFAULT_MAX_AGE = 60 * 60 * 24 * 7  # 7日


def _get_secret() -> str:
    secret = os.environ.get("SPA_TOKEN_SECRET")
    if not secret:
        raise ValueError("SPA_TOKEN_SECRET 環境変数が設定されていません")
    return secret


def generate_token(
    user_id: str, *, secret: str | None = None, max_age: int = DEFAULT_MAX_AGE
) -> str:
    """HMAC-SHA256 トークンを生成する。形式: {user_id}:{expiry_unix}:{hmac_hex}"""
    if secret is None:
        secret = _get_secret()
    expiry = int(time.time()) + max_age
    msg = f"{user_id}:{expiry}"
    sig = hmac.new(bytes.fromhex(secret), msg.encode(), hashlib.sha256).hexdigest()
    return f"{user_id}:{expiry}:{sig}"


def verify_token(token: str, *, secret: str | None = None) -> str | None:
    """トークンを検証し、有効なら user_id を返す。無効なら None。"""
    if secret is None:
        secret = _get_secret()
    parts = token.split(":")
    if len(parts) != 3:
        return None
    user_id, expiry_str, sig = parts
    try:
        expiry = int(expiry_str)
    except ValueError:
        return None
    if time.time() > expiry:
        return None
    msg = f"{user_id}:{expiry_str}"
    expected = hmac.new(bytes.fromhex(secret), msg.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return user_id


def spa_login(
    response,  # type: ignore
    user_id: str,
    *,
    secret: str | None = None,
    max_age: int = DEFAULT_MAX_AGE,
):
    """レスポンスに SPA トークン Cookie をセットする"""
    token = generate_token(user_id, secret=secret, max_age=max_age)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=max_age,
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/",
    )


def spa_logout(response):  # type: ignore
    """レスポンスから SPA トークン Cookie を削除する"""
    response.delete_cookie(COOKIE_NAME, path="/")
