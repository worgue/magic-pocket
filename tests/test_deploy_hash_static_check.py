"""deploy_hash route の STATIC_URL の形の検査 (KN1688)。

CloudFront Function は route の prefix 直下の 1 セグメントだけを hash として
外すため、STATIC_URL が `<prefix>{DEPLOY_HASH}/` の形でないと static が 403 になる。
"""

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core import checks

from pocket.django import checks as pocket_checks
from pocket.django.checks import deploy_hash_static_url_error
from pocket.django.utils import get_storages


@pytest.mark.parametrize(
    "static_url",
    [
        "static/abc1234/",
        "/static/abc1234/",
        "https://cdn.example.com/static/abc1234/",
    ],
)
def test_matching_static_url(static_url):
    assert deploy_hash_static_url_error(static_url, "/static/", "abc1234") is None


def test_custom_non_hex_deploy_hash():
    """git hash の形でない DEPLOY_HASH (Function が追加で外す値) でも通る"""
    assert deploy_hash_static_url_error("static/v1.2.3/", "/static/", "v1.2.3") is None


@pytest.mark.parametrize(
    "static_url",
    [
        # hash が prefix の直後に無い (Function が外せず 403)
        "static/v2/abc1234/",
        # hash を含まない (長期キャッシュで古いファイルが返る)
        "static/",
        # 別の prefix
        "assets/abc1234/",
    ],
)
def test_mismatching_static_url(static_url):
    message = deploy_hash_static_url_error(static_url, "/static/", "abc1234")
    assert message is not None
    assert "'/static/abc1234/'" in message
    assert 'STATIC_URL = f"static/{DEPLOY_HASH}/"' in message


def _run_check(monkeypatch, static_url, deploy_hash):
    monkeypatch.setattr(
        pocket_checks, "settings", SimpleNamespace(STATIC_URL=static_url)
    )
    env = {"DEPLOY_HASH": deploy_hash} if deploy_hash else {}
    with patch.dict(os.environ, env):
        if not deploy_hash:
            os.environ.pop("DEPLOY_HASH", None)
        return pocket_checks.check_deploy_hash_static_url()


def test_get_storages_registers_check(use_toml, monkeypatch):
    """get_storages() が route の prefix で system check を登録する"""
    monkeypatch.setattr(pocket_checks, "_deploy_hash_static", {})
    use_toml("tests/data/toml/django_deploy_hash.toml")
    get_storages(stage="dev")
    assert pocket_checks._deploy_hash_static == {"prefix": "/static/"}
    assert (
        pocket_checks.check_deploy_hash_static_url
        in checks.registry.registry.get_checks()
    )

    errors = _run_check(monkeypatch, "/static/v2/abc1234/", "abc1234")
    assert [e.id for e in errors] == ["pocket.E001"]
    assert _run_check(monkeypatch, "/static/abc1234/", "abc1234") == []
    # DEPLOY_HASH 未設定 (ローカル開発で `dev` に落ちる) は検査しない
    assert _run_check(monkeypatch, "/static/dev/", None) == []


def test_check_skipped_without_deploy_hash_route(use_toml, monkeypatch):
    """deploy_hash route を使わない staticfiles では何もしない"""
    monkeypatch.setattr(pocket_checks, "_deploy_hash_static", {})
    use_toml("tests/data/toml/default.toml")
    get_storages(stage="dev")
    assert pocket_checks._deploy_hash_static == {}
    assert _run_check(monkeypatch, "/static/", "abc1234") == []
