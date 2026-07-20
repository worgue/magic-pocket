"""SPA fallback_uri 組み立てのテスト。

prefix 付き SPA route (`path_pattern = "/admin/*"`) で glob の `*` がリテラル URI に
残ると (`/admin/*/index.html`)、拡張子なしリクエストが全て存在しないキーへ書き換え
られ SPA が一切配信できない (回帰テスト)。
"""

from __future__ import annotations

from types import SimpleNamespace

from pocket_cli.resources.aws.cloudformation import CloudFrontStack


def _route(path_pattern: str, spa_fallback_html: str = "index.html"):
    return SimpleNamespace(
        path_pattern=path_pattern, spa_fallback_html=spa_fallback_html
    )


def test_prefix_glob_pattern_strips_wildcard():
    """`/admin/*` → `/admin/index.html` (glob の `*` を残さない)"""
    assert CloudFrontStack._spa_fallback_uri(_route("/admin/*")) == "/admin/index.html"


def test_prefix_pattern_without_trailing_slash():
    """`/admin*` のような slash なし glob も prefix だけを使う"""
    assert CloudFrontStack._spa_fallback_uri(_route("/admin*")) == "/admin/index.html"


def test_catch_all_empty_pattern():
    """catch-all (path_pattern 空) は従来どおり `/index.html`"""
    assert CloudFrontStack._spa_fallback_uri(_route("")) == "/index.html"


def test_root_glob_pattern():
    """`/*` も `/index.html` に落ちる"""
    assert CloudFrontStack._spa_fallback_uri(_route("/*")) == "/index.html"


def test_custom_fallback_html():
    """spa_fallback_html の上書きにも prefix が効くこと"""
    assert (
        CloudFrontStack._spa_fallback_uri(_route("/admin/*", "app.html"))
        == "/admin/app.html"
    )
