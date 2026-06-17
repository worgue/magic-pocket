from __future__ import annotations

import hmac
import os

from .client_ip import parse_viewer_ip

# CloudFront → origin に付与される secret header の値が入る Lambda runtime env。
# pocket.context.ORIGIN_VERIFY_SECRET_KEY (managed secret のキー = env 名) と一致。
ORIGIN_VERIFY_SECRET_ENV = "POCKET_ORIGIN_VERIFY_SECRET"  # noqa: S105 env 名であって値ではない

# CloudFront origin custom header `X-Pocket-Origin-Verify` の Django META 表現。
# (cloudfront.yaml の OriginCustomHeaders と一致させること。)
ORIGIN_VERIFY_HEADER_META = "HTTP_X_POCKET_ORIGIN_VERIFY"

# CloudFront Function が載せる viewer IP header `X-Pocket-Viewer-Ip` の META 表現。
# (cf_function_api_host.js と一致させること。)
VIEWER_IP_HEADER_META = "HTTP_X_POCKET_VIEWER_IP"


class OriginVerifyMiddleware:
    """origin 直叩きを弾き、CloudFront 経由のときだけ真の client IP を
    `REMOTE_ADDR` に正規化する middleware。

    `enable_origin_verify = true` で deploy すると magic-pocket が:

    - CloudFront → origin に secret header (`X-Pocket-Origin-Verify`) を付与し、
      同値を Lambda runtime env (`POCKET_ORIGIN_VERIFY_SECRET`) に注入する。
    - CloudFront Function が詐称不可の viewer IP を `X-Pocket-Viewer-Ip` に載せる。

    挙動:

    - env secret が未設定 (= origin verify 無効。local / dev で CloudFront 無し)
      → **no-op**。生の `REMOTE_ADDR` を passthrough する。
    - env secret あり + request の secret header が一致 → CloudFront 経由とみなし、
      `X-Pocket-Viewer-Ip` をパースして `REMOTE_ADDR` を上書きする。
    - env secret あり + secret header が無い / 不一致 → **origin 直叩き**なので
      403 で弾く (`REMOTE_ADDR = None` は DRF throttle / django-axes 等の str 前提
      consumer を壊すため採らない)。理想は API Gateway 段で Django に到達させない。

    `REMOTE_ADDR` を読む既存資産 (django-axes / DRF throttling / ratelimit /
    access log) より前に走らせる必要があるため、`MIDDLEWARE` の **最前段** に置く:

        MIDDLEWARE = [
            "pocket.django.origin_verify.OriginVerifyMiddleware",
            "django.middleware.security.SecurityMiddleware",
            ...,
        ]
    """

    def __init__(self, get_response):  # type: ignore
        self.get_response = get_response

    def __call__(self, request):  # type: ignore
        secret = os.environ.get(ORIGIN_VERIFY_SECRET_ENV)
        if not secret:
            # origin verify 無効 (local/dev)。生 REMOTE_ADDR を尊重して no-op。
            return self.get_response(request)
        provided = request.META.get(ORIGIN_VERIFY_HEADER_META, "")
        if not hmac.compare_digest(provided, secret):
            return self._forbidden()
        ip = parse_viewer_ip(request.META.get(VIEWER_IP_HEADER_META, ""))
        if ip:
            request.META["REMOTE_ADDR"] = ip
        return self.get_response(request)

    @staticmethod
    def _forbidden():  # type: ignore
        from django.http import HttpResponseForbidden

        return HttpResponseForbidden(b"origin direct access is not allowed")
