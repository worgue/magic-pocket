import boto3
from moto import mock_aws

from pocket.django.runtime import get_django_settings
from pocket.runtime import get_secrets
from pocket.settings import Settings


@mock_aws
def test_secrets():
    settings = Settings.from_toml(stage="dev", path="tests/data/toml/default.toml")
    client = boto3.client("secretsmanager", region_name=settings.region)
    client.create_secret(
        Name="pocket/dev-testprj/DATABASE_URL",
        SecretString="postgres://localhost:5432",
    )
    print(get_secrets("dev", "tests/data/toml/default.toml"))


def test_django_settings():
    assert {
        "TEST_NESTED": {"first": {"second": {"third": {"NAME": "key"}}}}
    } == get_django_settings("dev", "tests/data/toml/default.toml")
