"""`pocket awscontainer reload-env` / `status-env` CLI のテスト。

`pocket waf ip` と同じ side-channel pattern で、SSM/SM の最新値を Lambda の
Environment.Variables に直接書き込む。
"""

from __future__ import annotations

from unittest import mock

from click.testing import CliRunner
from pocket_cli.cli.awscontainer_cli import awscontainer


def _fake_lambda_client(current_env: dict[str, str]):
    """get_function_configuration / update_function_configuration を mock した
    Lambda client を返す。update のキャプチャ用属性 `_updates` を持つ。"""
    client = mock.MagicMock()
    client.get_function_configuration.return_value = {
        "Environment": {"Variables": dict(current_env)},
    }
    client._updates = []  # type: ignore[attr-defined]

    def _capture_update(**kwargs):
        client._updates.append(kwargs)  # type: ignore[attr-defined]
        return {}

    client.update_function_configuration.side_effect = _capture_update
    return client


def _invoke(monkeypatch, command, *, secrets, lambda_client, use_toml):
    """共通 setup: get_secrets と boto3.client('lambda') を差し替えて CLI を実行。"""
    use_toml("tests/data/toml/default.toml")
    monkeypatch.setattr(
        "pocket_cli.cli.awscontainer_cli.get_secrets", lambda stage: secrets
    )
    original_boto3_client = __import__("boto3").client

    def _fake_boto3_client(service, **kwargs):
        if service == "lambda":
            return lambda_client
        return original_boto3_client(service, **kwargs)

    monkeypatch.setattr("boto3.client", _fake_boto3_client)
    runner = CliRunner()
    return runner.invoke(awscontainer, command)


# ---------------------------------------------------------------------------
# reload-env
# ---------------------------------------------------------------------------


def test_reload_env_updates_all_handlers(use_toml, monkeypatch):
    """全 handler に対して update_function_configuration が呼ばれる。"""
    secrets = {"SECRET_KEY": "fresh-value", "EMAIL_LOGIN_ENABLED": "true"}
    current = {
        "POCKET_STAGE": "dev",
        "SECRET_KEY": "stale-value",
        "EMAIL_LOGIN_ENABLED": "false",
    }
    client = _fake_lambda_client(current)
    result = _invoke(
        monkeypatch,
        ["reload-env", "--stage", "dev"],
        secrets=secrets,
        lambda_client=client,
        use_toml=use_toml,
    )
    assert result.exit_code == 0, result.output
    # default.toml は wsgi / sqsmanagement / management の 3 handler
    assert len(client._updates) >= 1
    # update した env には fresh secret 値 + 既存 POCKET_STAGE が両方入る
    for call in client._updates:
        env = call["Environment"]["Variables"]
        assert env["SECRET_KEY"] == "fresh-value"
        assert env["EMAIL_LOGIN_ENABLED"] == "true"
        assert env["POCKET_STAGE"] == "dev"  # 既存 env を保持


def test_reload_env_handler_filter(use_toml, monkeypatch):
    """--handler 指定で対象を絞れる。"""
    secrets = {"SECRET_KEY": "fresh"}
    client = _fake_lambda_client({"SECRET_KEY": "stale"})
    result = _invoke(
        monkeypatch,
        ["reload-env", "--stage", "dev", "--handler", "wsgi"],
        secrets=secrets,
        lambda_client=client,
        use_toml=use_toml,
    )
    assert result.exit_code == 0, result.output
    # wsgi 1 handler だけ呼ばれる
    assert len(client._updates) == 1
    # FunctionName は deploy 側と同じ正準名 resource_prefix + key
    # (= {stage}-{project}-{namespace}-{handler})。namespace (既定 `pocket`) を
    # 取りこぼさないことを検証する (旧実装は {slug}-{handler} で `-pocket-` が欠落)。
    assert client._updates[0]["FunctionName"] == "dev-testprj-pocket-wsgi"


def test_reload_env_handler_filter_invalid(use_toml, monkeypatch):
    """存在しない handler 指定はエラー。"""
    client = _fake_lambda_client({})
    result = _invoke(
        monkeypatch,
        ["reload-env", "--stage", "dev", "--handler", "nope"],
        secrets={"K": "v"},
        lambda_client=client,
        use_toml=use_toml,
    )
    assert result.exit_code != 0
    assert "nope" in result.output


def test_reload_env_skips_when_no_diff(use_toml, monkeypatch):
    """Lambda 側の env が既に最新と一致していれば update を呼ばない。"""
    secrets = {"SECRET_KEY": "same"}
    client = _fake_lambda_client({"POCKET_STAGE": "dev", "SECRET_KEY": "same"})
    result = _invoke(
        monkeypatch,
        ["reload-env", "--stage", "dev"],
        secrets=secrets,
        lambda_client=client,
        use_toml=use_toml,
    )
    assert result.exit_code == 0, result.output
    assert client._updates == []
    assert "差分なし" in result.output


def test_reload_env_no_secrets_declared_warns(use_toml, monkeypatch):
    """secrets が宣言されていなければ何もしない。"""
    client = _fake_lambda_client({})
    result = _invoke(
        monkeypatch,
        ["reload-env", "--stage", "dev"],
        secrets={},
        lambda_client=client,
        use_toml=use_toml,
    )
    assert result.exit_code == 0, result.output
    assert client._updates == []


def test_reload_env_lambda_not_deployed(use_toml, monkeypatch):
    """deploy 前で Lambda function が存在しない場合は明示エラー。"""
    from botocore.exceptions import ClientError

    client = mock.MagicMock()
    client.get_function_configuration.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "x"}},
        "GetFunctionConfiguration",
    )
    result = _invoke(
        monkeypatch,
        ["reload-env", "--stage", "dev"],
        secrets={"K": "v"},
        lambda_client=client,
        use_toml=use_toml,
    )
    assert result.exit_code != 0
    assert "pocket deploy" in result.output


# ---------------------------------------------------------------------------
# status-env
# ---------------------------------------------------------------------------


def test_status_env_no_drift(use_toml, monkeypatch):
    secrets = {"SECRET_KEY": "v"}
    client = _fake_lambda_client({"SECRET_KEY": "v"})
    result = _invoke(
        monkeypatch,
        ["status-env", "--stage", "dev"],
        secrets=secrets,
        lambda_client=client,
        use_toml=use_toml,
    )
    assert result.exit_code == 0, result.output
    assert "drift なし" in result.output
    assert client._updates == []


def test_status_env_detects_missing_and_stale(use_toml, monkeypatch):
    """Lambda に未反映なキー (+) と値が古いキー (~) を区別表示する。"""
    secrets = {"NEW_KEY": "new", "OLD_KEY": "fresh"}
    client = _fake_lambda_client({"OLD_KEY": "stale"})  # NEW_KEY 欠落 + OLD_KEY 古い
    result = _invoke(
        monkeypatch,
        ["status-env", "--stage", "dev"],
        secrets=secrets,
        lambda_client=client,
        use_toml=use_toml,
    )
    assert result.exit_code == 0, result.output
    assert "drift" in result.output
    assert "+ NEW_KEY" in result.output  # 未反映
    assert "~ OLD_KEY" in result.output  # 値が古い
    assert client._updates == []  # status は読み取り専用
