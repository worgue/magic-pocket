import os

import boto3
import pytest
from moto import mock_aws

import pocket.runtime as runtime
from pocket.django.runtime import get_django_settings
from pocket.runtime import _pocket_secret_to_envs, get_secrets, refresh_dsql_token
from pocket.settings import ManagedSecretSpec, Settings


@mock_aws
def test_secrets(use_toml):
    use_toml("tests/data/toml/default.toml")
    settings = Settings.from_toml(stage="dev")
    client = boto3.client("secretsmanager", region_name=settings.region)
    client.create_secret(
        Name="pocket/dev-testprj/DATABASE_URL",
        SecretString="postgres://localhost:5432",
    )
    print(get_secrets("dev"))


def test_django_settings(use_toml):
    use_toml("tests/data/toml/default.toml")
    assert {
        "TEST_NESTED": {"first": {"second": {"third": {"NAME": "key"}}}}
    } == get_django_settings("dev")


def test_refresh_dsql_token_no_endpoint(monkeypatch):
    monkeypatch.delenv("POCKET_DSQL_ENDPOINT", raising=False)
    monkeypatch.delenv("POCKET_DSQL_REGION", raising=False)
    monkeypatch.delenv("POCKET_DSQL_TOKEN", raising=False)
    assert refresh_dsql_token() is None
    assert "POCKET_DSQL_TOKEN" not in os.environ


def test_refresh_dsql_token_regenerates(monkeypatch):
    monkeypatch.setenv("POCKET_DSQL_ENDPOINT", "abc.dsql.ap-northeast-1.on.aws")
    monkeypatch.setenv("POCKET_DSQL_REGION", "ap-northeast-1")
    monkeypatch.setenv("POCKET_DSQL_TOKEN", "old-token")

    calls = {}

    class FakeDsqlClient:
        def generate_db_connect_admin_auth_token(self, endpoint, region):
            calls["token_args"] = (endpoint, region)
            return "fresh-token"

    def fake_client(service, region_name=None):
        calls["service"] = service
        calls["region_name"] = region_name
        return FakeDsqlClient()

    monkeypatch.setattr(runtime.boto3, "client", fake_client)

    token = refresh_dsql_token()
    assert token == "fresh-token"
    assert os.environ["POCKET_DSQL_TOKEN"] == "fresh-token"
    assert calls["service"] == "dsql"
    assert calls["region_name"] == "ap-northeast-1"
    assert calls["token_args"] == ("abc.dsql.ap-northeast-1.on.aws", "ap-northeast-1")


def test_pocket_secret_to_envs_rsa_options_default():
    """rsa_pem_base64 の options 省略時にデフォルトサフィックスが適用されること

    settings.ManagedSecretSpec のコメントおよび Rust 実装と同じ
    _PEM_BASE64 / _PUB_BASE64 が既定 (省略で Lambda init が KeyError になる回帰)。
    """
    spec = ManagedSecretSpec(type="rsa_pem_base64")
    envs = _pocket_secret_to_envs("KEY", {"pem": "p", "pub": "q"}, spec)
    assert envs == {"KEY_PEM_BASE64": "p", "KEY_PUB_BASE64": "q"}


def test_pocket_secret_to_envs_rsa_options_override():
    """options 明示時はその値が使われること"""
    spec = ManagedSecretSpec(
        type="cloudfront_signing_key",
        options={
            "pem_base64_environ_suffix": "_PRIV",
            "pub_base64_environ_suffix": "_PUB",
        },
    )
    envs = _pocket_secret_to_envs("SIGN", {"pem": "p", "pub": "q"}, spec)
    assert envs == {"SIGN_PRIV": "p", "SIGN_PUB": "q"}


def test_set_envs_from_secrets_retries_after_failure(use_toml, monkeypatch):
    """get_secrets が失敗した後の再呼び出しが silent no-op にならないこと

    以前は LOADED フラグを処理前に立てていたため、途中の例外後は
    フラグで即 return し DATABASE_URL 等が未設定のまま進行しえた。
    """
    use_toml("tests/data/toml/default.toml")
    monkeypatch.delenv("POCKET_ENVS_SECRETS_LOADED", raising=False)
    calls = {"n": 0}

    def _fail_once(stage):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated SSM throttling")
        return {"FROM_SECRETS": "ok"}

    monkeypatch.setattr(runtime, "get_secrets", _fail_once)
    monkeypatch.setattr(runtime, "_set_rds_database_url", lambda: None)
    monkeypatch.setattr(runtime, "_set_dsql_token", lambda: None)

    with pytest.raises(RuntimeError, match="simulated"):
        runtime.set_envs_from_secrets(stage="dev")
    assert os.environ.get("POCKET_ENVS_SECRETS_LOADED") != "true"

    runtime.set_envs_from_secrets(stage="dev")
    assert os.environ.get("FROM_SECRETS") == "ok"
    assert os.environ.get("POCKET_ENVS_SECRETS_LOADED") == "true"
