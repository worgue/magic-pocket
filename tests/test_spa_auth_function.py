"""SPA gate (require_token) の CloudFront Function のテスト。

cookie ライブラリによっては `pocket-spa-token` の値に含まれる ':' が %3A に
percent-encode される (axum-extra の CookieJar / cookie crate の `encoded()`)。
Function 側で decode してから分割しないと `parts.length !== 3` で常に 302 に
なるため、decode が入っていることを回帰テストで固定する (KN1458)。
"""

from __future__ import annotations

import yaml as yaml_lib
from pocket_cli.resources.aws.cloudformation import CloudFrontStack

from pocket.context import CloudFrontContext, RouteContext


def _spa_auth_function_body() -> str:
    ctx = CloudFrontContext(
        name="web",
        region="ap-northeast-1",
        s3_region="ap-northeast-1",
        stage="dev",
        domain="www.example.com",
        hosted_zone_id_override="ZPARENT",
        slug="dev-testprj-web",
        bucket_name="dev-testprj-bucket",
        resource_prefix="dev-testprj-",
        redirect_from=[],
        routes=[
            RouteContext(
                is_default=True, is_spa=True, require_token=True, login_path="/l"
            )
        ],
        token_secret="TOKEN",  # noqa: S106 (secret キー名であって値ではない)
    )
    stack = CloudFrontStack(ctx)
    stack._resolve_acm_arn = lambda: "arn:aws:acm:us-east-1:0:certificate/x"
    stack._resolve_waf_arn = lambda: None
    res = yaml_lib.safe_load(stack.yaml)["Resources"]
    fc = res["UrlFallbackFunctionRoot"]["Properties"]["FunctionCode"]
    return fc["Fn::Sub"][0] if isinstance(fc, dict) else fc


def test_spa_auth_function_decodes_cookie_value_before_split():
    """percent-encode された cookie 値 (%3A) を decode してから ':' で分割する"""
    body = _spa_auth_function_body()
    decode_at = body.index("decodeURIComponent(cookie.value)")
    split_at = body.index("value.split(':')")
    assert decode_at < split_at
    # 不正な percent-encoding は例外で 5xx にせず login へ redirect する
    assert "catch (e) { return _redirect(originalUri); }" in body
