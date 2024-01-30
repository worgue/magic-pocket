import boto3
from click.testing import CliRunner
from moto import mock_secretsmanager

from pocket import __version__
from pocket.cli.main import main, version
from pocket.context import Context
from pocket.settings import SecretsManager, Settings


def test_version():
    runner = CliRunner()
    result = runner.invoke(version)
    assert result.exit_code == 0
    assert result.output == f"{__version__}\n"
    result = runner.invoke(main, ["version"])
    assert result.exit_code == 0
    assert result.output == f"{__version__}\n"


def test_settings_from_toml():
    settings = Settings.from_toml(stage="dev", path="tests/data/toml/default.toml")
    assert settings.project_name == "testprj"


@mock_secretsmanager
def test_secretsmanager():
    settings = Settings.from_toml(stage="dev", path="tests/data/toml/default.toml")
    client = boto3.client("secretsmanager", region_name=settings.region)
    res = client.create_secret(
        Name="pocket/dev-testprj/DATABASE_URL",
        SecretString="postgres://localhost:5432",
    )
    assert settings.awscontainer
    settings.awscontainer.secretsmanager = SecretsManager(
        secrets={"DATABASE_URL": res["ARN"]}
    )
    context = Context.from_settings(settings)
    assert context.awscontainer
    assert context.awscontainer.secretsmanager
    assert context.awscontainer.secretsmanager.resource.secrets == {
        "DATABASE_URL": "postgres://localhost:5432"
    }


def test_neon():
    settings = Settings.from_toml(stage="dev", path="tests/data/toml/default.toml")
    assert not settings.neon
    context = Context.from_settings(settings)
    assert not context.neon
    runner = CliRunner()
