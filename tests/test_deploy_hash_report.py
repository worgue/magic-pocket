"""deploy 時の DEPLOY_HASH 可視化 (`deploy_hash_report`) のテスト。

DEPLOY_HASH env の伝播漏れで黙って git short hash に落ちる footgun を deploy 時に
可視化するためのメッセージ生成を検証する。出所 (env / git HEAD) を取り違えない
ことと、deploy_hash route が無い構成では何も出さないことを確認する。
"""

from __future__ import annotations

import os
from unittest import mock

from pocket.context import Context, deploy_hash_report


def test_report_shows_env_source_when_env_set(use_toml):
    """DEPLOY_HASH env がある場合、その値と「env より」を明示する。"""
    with mock.patch.dict(os.environ, {"DEPLOY_HASH": "adoc167-cd9ee30-01"}):
        use_toml("tests/data/toml/cloudfront_deploy_hash.toml")
        context = Context.from_toml(stage="dev")
        message = deploy_hash_report(context)
    assert message is not None
    assert "adoc167-cd9ee30-01" in message
    assert "DEPLOY_HASH env より" in message


def test_report_shows_git_fallback_when_env_unset(use_toml):
    """DEPLOY_HASH env が無い場合、git HEAD 由来である旨と明示を促す。"""
    env = {k: v for k, v in os.environ.items() if k != "DEPLOY_HASH"}
    with mock.patch.dict(os.environ, env, clear=True):
        use_toml("tests/data/toml/cloudfront_deploy_hash.toml")
        context = Context.from_toml(stage="dev")
        message = deploy_hash_report(context)
    assert message is not None
    assert "git HEAD" in message
    assert "未設定" in message  # 明示を促す文言


def test_report_is_none_without_deploy_hash_route(use_toml):
    """deploy_hash versioning の route が無ければ DEPLOY_HASH は無関係 → None。"""
    use_toml("tests/data/toml/default.toml")
    context = Context.from_toml(stage="dev")
    assert deploy_hash_report(context) is None
