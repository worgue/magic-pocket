"""wsgi_handler の PATH_INFO transcode (非 ASCII パスの 500 防止)。

API Gateway payload v1 は event["path"] をパーセントデコード済みで渡すため、
apig_wsgi の PATH_INFO に生の非 ASCII が入り、Django の latin-1 検証
(get_bytes_from_wsgi の value.encode("iso-8859-1")) で UnicodeEncodeError → 500 に
なる。lambda_handlers._wsgi_transcode_path が WSGI 流 transcode に直すことを検証する。
"""

import sys

import pytest


@pytest.fixture
def lambda_handlers(monkeypatch):
    # lambda_handlers はインポート時に make_lambda_handler(get_wsgi_application())
    # を実行し {project}.wsgi の import を要求する。テストでは wsgi app 生成を差し替え、
    # モジュールキャッシュを落としてから import し直す。
    import pocket.utils

    monkeypatch.setattr(
        pocket.utils, "get_wsgi_application", lambda: lambda environ, sr: [b""]
    )
    monkeypatch.delitem(sys.modules, "pocket.django.lambda_handlers", raising=False)
    import pocket.django.lambda_handlers as m

    return m


def test_transcode_non_ascii_path(lambda_handlers):
    # payload v1: PATH_INFO に生の非 ASCII (乃) が入っている状態
    environ = {"PATH_INFO": "/api/favorites/乃", "SCRIPT_NAME": ""}
    out = lambda_handlers._wsgi_transcode_path(environ)
    # latin-1 で encode 可能 (= WSGI 準拠) になり、Django が元の path を復元できる
    out["PATH_INFO"].encode("iso-8859-1")  # UnicodeEncodeError を出さない
    assert out["PATH_INFO"].encode("iso-8859-1").decode("utf-8") == "/api/favorites/乃"


def test_ascii_path_unchanged(lambda_handlers):
    environ = {"PATH_INFO": "/api/favorites/123", "SCRIPT_NAME": ""}
    out = lambda_handlers._wsgi_transcode_path(environ)
    assert out["PATH_INFO"] == "/api/favorites/123"


def test_latin1_safe_path_not_double_encoded(lambda_handlers):
    # payload v2 の rawPath 由来 (unquote 済みで既に latin-1 safe) は素通しし、
    # 二重エンコードしない
    path = "乃".encode("utf-8").decode("iso-8859-1")  # 既に transcode 済みの形
    environ = {"PATH_INFO": f"/api/{path}", "SCRIPT_NAME": ""}
    out = lambda_handlers._wsgi_transcode_path(environ)
    assert out["PATH_INFO"] == f"/api/{path}"


def test_transcode_script_name(lambda_handlers):
    environ = {"PATH_INFO": "/x", "SCRIPT_NAME": "/乃"}
    out = lambda_handlers._wsgi_transcode_path(environ)
    assert out["SCRIPT_NAME"].encode("iso-8859-1").decode("utf-8") == "/乃"
