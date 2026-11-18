import pytest
from pocket_cli.cli.deploy_cli import get_resources

from pocket.context import ContainerContext, Context, SecretsContext
from pocket.general_context import GeneralContext


def test_context_from_settings(base_settings):
    context = Context.from_settings(base_settings)
    assert context.general
    assert context.general.project_name == base_settings.project_name
    assert context.general.region == base_settings.region
    assert context.general.namespace == base_settings.namespace


def test_context_from_toml(use_toml, tmp_path):
    toml_path = tmp_path / "pocket.toml"
    toml_path.write_text(
        """
[general]
region = "us-east-1"
project_name = "test-project"
stages = ["dev"]
namespace = "test"
"""
    )
    use_toml(str(toml_path))
    context = Context.from_toml(stage="dev")
    assert context.general
    assert context.general.project_name == "test-project"
    assert context.general.region == "us-east-1"
    assert context.general.namespace == "test"


def test_general_context_from_settings(base_settings):
    context = GeneralContext.from_general_settings(base_settings.general)
    assert context.project_name == base_settings.general.project_name
    assert context.region == base_settings.general.region
    assert context.namespace == base_settings.general.namespace


def test_general_context_from_toml(use_toml, tmp_path):
    toml_path = tmp_path / "pocket.toml"
    toml_path.write_text(
        """
[general]
region = "us-east-1"
project_name = "test-project"
stages = ["dev"]
namespace = "test"
"""
    )
    use_toml(str(toml_path))
    context = GeneralContext.from_toml()
    assert context.project_name == "test-project"
    assert context.region == "us-east-1"
    assert context.namespace == "test"


def _toml_with_neon(extra: str = "") -> str:
    return f"""
[general]
region = "us-east-1"
project_name = "test-project"
stages = ["dev"]
namespace = "test"

[dev.neon]
project_name = "dev-test-project"{extra}
"""


@pytest.mark.parametrize("mode", ["deploy", "command"])
def test_provisioning_is_rejected(use_toml, tmp_path, mode):
    """廃止した provisioning が残っていたら、値によらず案内つきで止める。"""
    toml_path = tmp_path / "pocket.toml"
    toml_path.write_text(_toml_with_neon(f'\nprovisioning = "{mode}"'))
    use_toml(str(toml_path))
    with pytest.raises(ValueError, match="provisioning は廃止"):
        Context.from_toml(stage="dev")


def test_skip_check_existing_is_rejected(use_toml, tmp_path):
    """廃止済み skip_check_existing が残っていたら fail-fast (黙殺しない)。"""
    toml_path = tmp_path / "pocket.toml"
    toml_path.write_text(_toml_with_neon("\nskip_check_existing = true"))
    use_toml(str(toml_path))
    with pytest.raises(ValueError, match="skip_check_existing は廃止"):
        Context.from_toml(stage="dev")


@pytest.mark.parametrize(
    ("section", "match"),
    [
        ('[dev.neon]\nproject_name = "n"\nneon_api_key = "k"', "neon_api_key は廃止"),
        ('[dev.tidb]\ncluster = "c1"', "cluster は廃止"),
        ("[dev.upstash]\nbudget = 10", r"\[upstash\] は廃止"),
        (
            '[container.main]\ndockerfile_path = "D"\n'
            "[container.main.secrets.user]\n"
            'DATABASE_URL = { type = "tidb_database_url" }',
            'tidb_database_url" は廃止',
        ),
    ],
)
def test_removed_provider_settings_are_rejected(use_toml, tmp_path, section, match):
    """廃止した key / 節 / type が残っていたら、直し方を添えて止める。"""
    toml_path = tmp_path / "pocket.toml"
    toml_path.write_text(
        '[general]\nregion = "us-east-1"\nproject_name = "p"\nstages = ["dev"]\n'
        + section
        + "\n"
    )
    use_toml(str(toml_path))
    with pytest.raises(ValueError, match=match):
        Context.from_toml(stage="dev")


def test_api_key_in_environment_is_not_loaded(use_toml, monkeypatch):
    """環境変数に key があっても、設定にも context にも載らない。"""
    monkeypatch.setenv("NEON_API_KEY", "should-not-be-loaded")
    use_toml("tests/data/toml/db_command_provisioning.toml")
    context = Context.from_toml(stage="dev")
    assert "should-not-be-loaded" not in context.model_dump_json()


def test_get_resources_excludes_provider_databases(use_toml):
    """Neon / TiDB は deploy のリソースに乗らない (API key 不要)。"""
    use_toml("tests/data/toml/db_command_provisioning.toml")
    context = Context.from_toml(stage="dev")
    assert context.neon and context.tidb
    names = {type(r).__name__ for r in get_resources(context)}
    assert not names & {"Neon", "TiDb"}


def _build_awscontainer_context(
    base_settings, *, permissions_boundary: str | None = None
) -> ContainerContext:
    """permissions_boundary 検証用の最小 ContainerContext を構築する。"""
    from pocket import settings

    awscontainer = settings.Container.model_validate(
        {
            "dockerfile_path": "Dockerfile",
            "handlers": {
                "wsgi": {"command": "pocket.django.lambda_handlers.wsgi_handler"},
            },
            **(
                {"permissions_boundary": permissions_boundary}
                if permissions_boundary is not None
                else {}
            ),
        }
    )
    base_settings.container["main"] = awscontainer
    return ContainerContext.from_settings("main", awscontainer, base_settings)


def test_awscontainer_permissions_boundary_from_toml(base_settings, monkeypatch):
    """toml の permissions_boundary が Context に反映されること。"""
    monkeypatch.delenv("POCKET_PERMISSIONS_BOUNDARY_ARN", raising=False)
    arn = "arn:aws:iam::123456789012:policy/test-boundary"
    ctx = _build_awscontainer_context(base_settings, permissions_boundary=arn)
    assert ctx.permissions_boundary == arn


def test_awscontainer_permissions_boundary_env_overrides_toml(
    base_settings, monkeypatch
):
    """env が toml より優先されること。"""
    env_arn = "arn:aws:iam::123456789012:policy/env-boundary"
    toml_arn = "arn:aws:iam::123456789012:policy/toml-boundary"
    monkeypatch.setenv("POCKET_PERMISSIONS_BOUNDARY_ARN", env_arn)
    ctx = _build_awscontainer_context(base_settings, permissions_boundary=toml_arn)
    assert ctx.permissions_boundary == env_arn


def test_awscontainer_permissions_boundary_none(base_settings, monkeypatch):
    """env も toml も未設定なら None になること。"""
    monkeypatch.delenv("POCKET_PERMISSIONS_BOUNDARY_ARN", raising=False)
    ctx = _build_awscontainer_context(base_settings)
    assert ctx.permissions_boundary is None


def test_secrets_user_name_format_placeholder(base_settings):
    """user secret の name に {stage}/{project}/{namespace} が format される。"""
    from pocket import settings

    secrets = settings.Secrets(
        user={
            "TOKEN": settings.UserSecretSpec(name="/svc/{stage}-token", store="ssm"),
        }
    )
    ctx = SecretsContext.from_settings(secrets, base_settings)
    # base_settings: stage="test", project_name="testprj", namespace="pocket"
    assert ctx.user["TOKEN"].name == "/svc/test-token"


def test_secrets_user_name_plain_is_noop(base_settings):
    """placeholder を含まない既存 name は format 後も同一 (後方互換)。"""
    from pocket import settings

    arn = "arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:my-secret"
    secrets = settings.Secrets(
        user={"API_KEY": settings.UserSecretSpec(name=arn, store="sm")},
    )
    ctx = SecretsContext.from_settings(secrets, base_settings)
    assert ctx.user["API_KEY"].name == arn


def test_dsql_context_endpoint_secret_name_defaults(base_settings):
    """[dsql] のみ (secrets 未設定) では既定の sm / 既定 key format で導出。"""
    from pocket import settings
    from pocket.context import DsqlContext

    base_settings.dsql = settings.Dsql()
    ctx = DsqlContext.from_settings(base_settings.dsql, base_settings)
    # base_settings: stage="test", project_name="testprj", namespace="pocket"
    assert ctx.endpoint_secret_name == "test-testprj-pocket-user/dsql_endpoint"
    assert ctx.endpoint_secret_store == "sm"


def test_dsql_context_endpoint_secret_name_follows_secrets_store(base_settings):
    """[container.main.secrets] store = "ssm" なら SSM パス (先頭 /) で導出。"""
    from pocket import settings
    from pocket.context import DsqlContext

    base_settings.dsql = settings.Dsql()
    base_settings.container["main"] = settings.Container(
        dockerfile_path="Dockerfile", secrets=settings.Secrets(store="ssm")
    )
    ctx = DsqlContext.from_settings(base_settings.dsql, base_settings)
    assert ctx.endpoint_secret_name == "/test-testprj-pocket-user/dsql_endpoint"
    assert ctx.endpoint_secret_store == "ssm"
