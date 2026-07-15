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
    if ":" in user_id:
        # トークン形式の区切りと衝突し、verify_token で常に無効になる。
        # 黙って発行すると毎レスポンス再発行 + redirect ループが恒久化する
        raise ValueError("user_id must not contain ':' (token format delimiter)")
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


class SpaTokenCookieMiddleware:
    """SPA token cookie の self-heal middleware。

    認証済み response に対しては cookie が無い / 期限切れなら token を発行し、
    未認証 response に対しては cookie があれば削除する。`AuthenticationMiddleware`
    の後に配置する。

    これがないと「Django session は生きているが SPA token は期限切れ」の状態
    (デフォルト設定で SESSION_COOKIE_AGE=14日 vs SPA token DEFAULT_MAX_AGE=7日
    のため、8日目以降に必ず発生) で `require_token` ルートにアクセスした際、
    CloudFront Function → login_path → 既ログイン判定で素通り → 元 URL へ
    bounce → token 無 → login_path へ … の無限 redirect ループに陥る。

    middleware を入れておくと、bounce response 経路に必ず通るため、その 1 往復
    で token cookie が補充されてループが断ち切れる。

    使い方:

        MIDDLEWARE = [
            ...,
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            "pocket.django.spa_auth.SpaTokenCookieMiddleware",
            ...,
        ]

    `SPA_TOKEN_SECRET` 環境変数が未設定の環境 (gating 未 deploy のローカル等)
    では no-op として動く。

    拡張ポイント (subclass で override):

    - `_should_issue(request)`: token を (再) 発行すべきかの判定。デフォルト
      は「cookie が無い or `verify_token` で失効判定」のみ。残り寿命が短い
      時にも発行する sliding refresh 等は subclass で表現する
    - `_max_age()`: 発行時の token 寿命 (秒)。デフォルトは `DEFAULT_MAX_AGE`
      (7 日)。短命 token を使う場合は subclass で settings 等から返す
    """

    def __init__(self, get_response):  # type: ignore
        self.get_response = get_response

    def __call__(self, request):  # type: ignore
        response = self.get_response(request)
        if not os.environ.get("SPA_TOKEN_SECRET"):
            return response
        if request.user.is_authenticated:
            if self._should_issue(request):
                spa_login(response, str(request.user.pk), max_age=self._max_age())
        elif COOKIE_NAME in request.COOKIES:
            spa_logout(response)
        return response

    def _should_issue(self, request) -> bool:  # type: ignore
        """token を (再) 発行すべきかの判定。

        デフォルトは「cookie 無 or 失効」で発行。残り寿命が短いときも発行する
        sliding refresh が欲しい場合は subclass で:

            def _should_issue(self, request):
                if super()._should_issue(request):
                    return True
                token = request.COOKIES[COOKIE_NAME]
                remaining = int(token.split(":")[1]) - time.time()
                return remaining < self._max_age() / 2
        """
        token = request.COOKIES.get(COOKIE_NAME)
        if token is None:
            return True
        # 失効だけでなく「別ユーザーの token」も再発行する (logout を挟まない
        # アカウント切替後に旧ユーザーの token が最長 7 日残存するのを防ぐ)
        return verify_token(token) != str(request.user.pk)

    def _max_age(self) -> int:
        """発行時の token 寿命 (秒)。subclass で settings 等から返せる。"""
        return DEFAULT_MAX_AGE
