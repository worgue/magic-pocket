import boto3
import pytest
from click.testing import CliRunner
from moto import mock_aws

from pocket import __version__
from pocket.cli.main_cli import main, version
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


@mock_aws
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
    assert context.awscontainer.secretsmanager.resource.resolved_secrets == {
        "DATABASE_URL": "postgres://localhost:5432"
    }


def get_default_awscontainer():
    context = Context.from_toml(stage="dev", path="tests/data/toml/default.toml")
    assert context.awscontainer
    assert context.awscontainer.resource.repository
    return context.awscontainer.resource


@mock_aws
def test_ecr():
    ac = get_default_awscontainer()
    ac.repository.ensure_exists()
    assert ac.repository.repository_uri


@mock_aws
def test_neon_none():
    settings = Settings.from_toml(stage="prd", path="tests/data/toml/default.toml")
    assert not settings.neon
    context = Context.from_settings(settings)
    assert not context.neon


@pytest.mark.skip(reason="Requires API key and manual deletion of the resource.")
@mock_aws
def test_neon_default():
    settings = Settings.from_toml(stage="dev", path="tests/data/toml/default.toml")
    assert settings.neon
    context = Context.from_settings(settings)
    assert context.neon
    assert context.neon.resource.status == "NOEXIST"
    context.neon.resource.create()
    assert context.neon.resource.status == "COMPLETED"
    context = Context.from_toml(stage="dev", path="tests/data/toml/default.toml")
    assert context.neon and context.neon.resource.status == "COMPLETED"
    raise Exception("Test worked well, but you have to delete the resource manually.")
