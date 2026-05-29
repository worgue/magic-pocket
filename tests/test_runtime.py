import os

import boto3
from moto import mock_aws

import pocket.runtime as runtime
from pocket.django.runtime import get_django_settings
from pocket.runtime import get_secrets, refresh_dsql_token
from pocket.settings import Settings


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
