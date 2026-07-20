"""ECR repo 名 / image タグの正準導出 (pocket.naming) と image CLI のテスト。

外部ツールが import して依存する契約なので導出結果を固定し、deploy 側
(AwsContainerContext.ecr_name) との一致を回帰テストで保証する。
"""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock, patch

from click.testing import CliRunner
from moto import mock_aws
from pocket_cli.cli.image_cli import image
from pocket_cli.resources.awscontainer import AwsContainer

import pocket
from pocket.context import Context
from pocket.naming import ecr_image_tag, ecr_repo_name


def test_ecr_repo_name_default():
    assert (
        ecr_repo_name(project="myprj", stage="sandbox") == "sandbox-myprj-pocket-lambda"
    )


def test_ecr_repo_name_custom_namespace_and_template():
    assert (
        ecr_repo_name(
            project="myprj",
            stage="dev",
            namespace="ns",
            prefix_template="{project}-{stage}-{namespace}-",
        )
        == "myprj-dev-ns-lambda"
    )


def test_ecr_repo_name_explicit_override_wins():
    """[awscontainer].ecr_name 明示上書きの構成では渡された値がそのまま返る"""
    assert (
        ecr_repo_name(project="myprj", stage="dev", ecr_name="shared-repo")
        == "shared-repo"
    )


def test_ecr_image_tag_is_stage():
    assert ecr_image_tag("sandbox") == "sandbox"


def test_exposed_at_package_root():
    assert pocket.ecr_repo_name is ecr_repo_name
    assert pocket.ecr_image_tag is ecr_image_tag


def _write_toml(tmp_path, extra: str = ""):
    toml_path = tmp_path / "pocket.toml"
    toml_path.write_text(
        f"""
[general]
region = "ap-southeast-1"
project_name = "testprj"
stages = ["dev"]

[awscontainer]
dockerfile_path = "Dockerfile"
{extra}

[awscontainer.handlers.wsgi]
command = "pocket.django.lambda_handlers.wsgi_handler"
"""
    )
    return toml_path


@mock_aws
def test_naming_matches_context_derivation(use_toml, tmp_path):
    """naming の導出が deploy 側 (context.ecr_name) と一致すること (drift 回帰)"""
    use_toml(str(_write_toml(tmp_path)))
    context = Context.from_toml(stage="dev")
    assert context.awscontainer
    assert context.awscontainer.ecr_name == ecr_repo_name(
        project="testprj", stage="dev"
    )


@mock_aws
def test_image_repo_outputs_ecr_name(use_toml, tmp_path):
    """image repo は ecr_name 上書きを含め toml 準拠の repo 名を出力する"""
    use_toml(str(_write_toml(tmp_path, 'ecr_name = "shared-repo"')))
    result = CliRunner().invoke(image, ["repo", "--stage", "dev"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "shared-repo"


@mock_aws
def test_image_uri_outputs_digest_pinned_uri(use_toml, tmp_path):
    """image uri は {repo_uri}@{digest} を stdout に出力する"""
    use_toml(str(_write_toml(tmp_path)))
    fake_ecr = MagicMock()
    fake_ecr.uri = (
        "123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/dev-testprj-pocket-lambda"
    )
    fake_ecr.image_detail.image_digest = "sha256:" + "a" * 64
    with patch.object(
        AwsContainer, "ecr", new_callable=PropertyMock, return_value=fake_ecr
    ):
        result = CliRunner().invoke(image, ["uri", "--stage", "dev"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "%s@%s" % (fake_ecr.uri, "sha256:" + "a" * 64)


@mock_aws
def test_image_uri_fails_loud_when_image_missing(use_toml, tmp_path):
    """deploy 前 (tag=stage の image なし) は legible エラーで中断する"""
    use_toml(str(_write_toml(tmp_path)))
    fake_ecr = MagicMock()
    fake_ecr.uri = (
        "123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/dev-testprj-pocket-lambda"
    )
    fake_ecr.image_detail.image_digest = None
    with patch.object(
        AwsContainer, "ecr", new_callable=PropertyMock, return_value=fake_ecr
    ):
        result = CliRunner().invoke(image, ["uri", "--stage", "dev"])
    assert result.exit_code != 0
    assert "deploy" in result.output
