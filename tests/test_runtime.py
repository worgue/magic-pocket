import boto3
from moto import mock_aws

from pocket.django.runtime import get_django_settings
from pocket.runtime import get_secrets
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
