"""deploy 完了後に Lambda env の DEPLOY_HASH が CFn 経由の更新スキップに依らず
同期されること (AwsContainer.ensure_post_deploy_state) のテスト。

回帰: `update()` が update_function_code (code のみ) で Environment を更新せず、
stack.update() が yaml_synced / wait_status timeout でスキップされると DEPLOY_HASH
が旧値に固着し、Django が古い hash の static URL を生成して CloudFront 側 (毎 deploy
追従) と乖離 → 静的アセット全滅 (403) という不具合の修正。
"""

from __future__ import annotations

import os
from unittest import mock

from moto import mock_aws
from pocket_cli.resources.awscontainer import AwsContainer

from pocket.context import Context


def _fake_lambda_client(current_env: dict[str, str]):
    """get_function / get_function_configuration / update_function_configuration を
    mock した Lambda client。update のキャプチャ用属性 `_updates` を持つ。"""
    client = mock.MagicMock()
    client.get_function.return_value = {
        "Configuration": {"CodeSha256": "deadbeef", "LastUpdateStatus": "Successful"},
    }
    client.get_function_configuration.return_value = {
        "Environment": {"Variables": dict(current_env)},
    }
    client._updates = []  # type: ignore[attr-defined]

    def _capture_update(**kwargs):
        client._updates.append(kwargs)  # type: ignore[attr-defined]
        return {}

    client.update_function_configuration.side_effect = _capture_update
    return client


def _build_container(use_toml, lambda_client, deploy_hash="53e8c22"):
    with mock_aws():
        with mock.patch.dict(os.environ, {"DEPLOY_HASH": deploy_hash}):
            use_toml("tests/data/toml/cloudfront_deploy_hash.toml")
            context = Context.from_toml(stage="dev")
    assert context.awscontainer
    original_boto3_client = __import__("boto3").client

    def _fake_boto3_client(service, **kwargs):
        if service == "lambda":
            return lambda_client
        return original_boto3_client(service, **kwargs)

    with mock.patch("boto3.client", _fake_boto3_client):
        ac = AwsContainer(context=context.awscontainer)
        yield_ac = ac
    return yield_ac, _fake_boto3_client


def test_post_deploy_syncs_stale_deploy_hash(use_toml, monkeypatch):
    """Lambda env の DEPLOY_HASH が旧値なら update_function_configuration で同期。"""
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)
    client = _fake_lambda_client(
        {"POCKET_STAGE": "dev", "DEPLOY_HASH": "91bae0c", "SECRET_KEY": "keep-me"}
    )
    ac, fake_client = _build_container(use_toml, client, deploy_hash="53e8c22")

    with mock.patch("boto3.client", fake_client):
        ac.ensure_post_deploy_state()

    assert len(client._updates) == 1
    new_env = client._updates[0]["Environment"]["Variables"]
    assert new_env["DEPLOY_HASH"] == "53e8c22"  # 新 hash に追従
    assert new_env["SECRET_KEY"] == "keep-me"  # 既存 env (secret 等) は保持
    assert new_env["POCKET_STAGE"] == "dev"


def test_post_deploy_skips_when_in_sync(use_toml, monkeypatch):
    """Lambda env が既に最新 hash なら update を呼ばない (冪等)。"""
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)
    client = _fake_lambda_client({"POCKET_STAGE": "dev", "DEPLOY_HASH": "53e8c22"})
    ac, fake_client = _build_container(use_toml, client, deploy_hash="53e8c22")

    with mock.patch("boto3.client", fake_client):
        ac.ensure_post_deploy_state()

    assert client._updates == []


def test_post_deploy_noop_without_deploy_hash(use_toml, monkeypatch):
    """deploy_hash route が無い構成では env を一切触らない。"""
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)
    client = _fake_lambda_client({"POCKET_STAGE": "dev"})
    with mock_aws():
        use_toml("tests/data/toml/default.toml")
        context = Context.from_toml(stage="dev")
    assert context.awscontainer
    original_boto3_client = __import__("boto3").client

    def _fake_boto3_client(service, **kwargs):
        if service == "lambda":
            return client
        return original_boto3_client(service, **kwargs)

    with mock.patch("boto3.client", _fake_boto3_client):
        ac = AwsContainer(context=context.awscontainer)
        ac.ensure_post_deploy_state()

    assert client._updates == []
