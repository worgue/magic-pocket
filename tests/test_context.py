from pocket.context import AwsContainerContext, Context, SecretsContext
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


def _build_awscontainer_context(
    base_settings, *, permissions_boundary: str | None = None
) -> AwsContainerContext:
    """permissions_boundary 検証用の最小 AwsContainerContext を構築する。"""
    from pocket import settings

    awscontainer = settings.AwsContainer.model_validate(
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
    base_settings.awscontainer = awscontainer
    return AwsContainerContext.from_settings(awscontainer, base_settings)


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
