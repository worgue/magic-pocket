import boto3
import pytest
from click.testing import CliRunner
from moto import mock_aws
from pocket_cli.cli.main_cli import main, version
from pocket_cli.mediator import Mediator
from pocket_cli.resources.awscontainer import AwsContainer
from pocket_cli.resources.neon import Neon

from pocket import __version__
from pocket.context import Context
from pocket.settings import Secrets, Settings, UserSecretSpec


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
    settings.awscontainer.secrets = Secrets(
        user={"DATABASE_URL": UserSecretSpec(name=res["ARN"], store="sm")}
    )
    context = Context.from_settings(settings)
    assert context.awscontainer
    assert context.awscontainer.secrets
    # user secretはSM経由で直接取得できる
    value = client.get_secret_value(SecretId=res["ARN"])
    assert value["SecretString"] == "postgres://localhost:5432"


@mock_aws
def test_initial_secretsmanager_policy():
    settings = Settings.from_toml(
        stage="prd", path="tests/data/toml/awscontainer_pocket_secrets.toml"
    )
    context = Context.from_settings(settings)
    assert context.awscontainer
    assert context.awscontainer.secrets
    assert context.awscontainer.secrets.allowed_sm_resources == []
    mediator = Mediator(context)
    mediator.ensure_pocket_managed_secrets()
    assert context.awscontainer.secrets.allowed_sm_resources != []


@mock_aws
def test_initial_ssm_policy():
    settings = Settings.from_toml(
        stage="prd", path="tests/data/toml/awscontainer_secrets_ssm.toml"
    )
    context = Context.from_settings(settings)
    assert context.awscontainer
    assert context.awscontainer.secrets
    assert context.awscontainer.secrets.store == "ssm"
    # SSMのmanagedなので、allowed_ssm_resourcesにパターンが入る
    assert context.awscontainer.secrets.allowed_ssm_resources != []
    # SMリソースはない
    assert context.awscontainer.secrets.allowed_sm_resources == []


@mock_aws
def test_ssm_pocket_secrets():
    settings = Settings.from_toml(
        stage="prd", path="tests/data/toml/awscontainer_secrets_ssm.toml"
    )
    context = Context.from_settings(settings)
    assert context.awscontainer
    assert context.awscontainer.secrets
    mediator = Mediator(context)
    mediator.ensure_pocket_managed_secrets()
    sc = context.awscontainer.secrets
    assert sc.pocket_store.secrets != {}
    assert "SECRET_KEY" in sc.pocket_store.secrets
    assert "DJANGO_SUPERUSER_PASSWORD" in sc.pocket_store.secrets


def get_default_awscontainer():
    context = Context.from_toml(stage="dev", path="tests/data/toml/default.toml")
    assert context.awscontainer
    ac = AwsContainer(context.awscontainer)
    assert ac.ecr
    return ac


@mock_aws
def test_ecr():
    ac = get_default_awscontainer()
    ac.ecr.ensure_exists()
    assert ac.ecr.uri


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
    neon = Neon(context.neon)
    assert neon.status == "NOEXIST"
    neon.create()
    assert neon.status == "COMPLETED"
    context = Context.from_toml(stage="dev", path="tests/data/toml/default.toml")
    assert context.neon and Neon(context.neon).status == "COMPLETED"
    raise Exception("Test worked well, but you have to delete the resource manually.")
