"""platform → Lambda Architectures 導出のテスト。

`platform = "linux/arm64"` でビルド・プッシュした arm64 イメージに対し、CFn
テンプレートが Architectures を出さないと Lambda は常に x86_64 で作成され、
起動時に exec format error (Runtime.InvalidEntrypoint) になる (回帰テスト)。
"""

from __future__ import annotations

import pytest
import yaml
from moto import mock_aws
from pocket_cli.resources.aws.cloudformation import ContainerStack
from pydantic import ValidationError

from pocket.context import Context
from pocket.settings import AwsContainer


def _write_platform_toml(tmp_path, platform_line: str = ""):
    toml_path = tmp_path / "pocket.toml"
    toml_path.write_text(
        f"""
[general]
region = "ap-southeast-1"
project_name = "testprj"
stages = ["dev"]

[awscontainer]
dockerfile_path = "Dockerfile"
{platform_line}

[awscontainer.handlers.wsgi]
command = "pocket.django.lambda_handlers.wsgi_handler"
"""
    )
    return toml_path


def _lambda_properties(yaml_str: str) -> dict:
    parsed = yaml.safe_load(yaml_str)
    return parsed["Resources"]["WsgiLambdaFunction"]["Properties"]


@mock_aws
def test_default_platform_is_x86_64(use_toml, tmp_path):
    """platform 省略 (linux/amd64) 時は x86_64 を明示する"""
    use_toml(str(_write_platform_toml(tmp_path)))
    context = Context.from_toml(stage="dev")
    assert context.awscontainer
    props = _lambda_properties(ContainerStack(context.awscontainer).yaml)
    assert props["Architectures"] == ["x86_64"]


@mock_aws
def test_arm64_platform_sets_arm64_architecture(use_toml, tmp_path):
    """platform = "linux/arm64" なら Lambda も arm64 で作成される"""
    use_toml(str(_write_platform_toml(tmp_path, 'platform = "linux/arm64"')))
    context = Context.from_toml(stage="dev")
    assert context.awscontainer
    props = _lambda_properties(ContainerStack(context.awscontainer).yaml)
    assert props["Architectures"] == ["arm64"]


def test_unknown_platform_fails_loud():
    """未知の platform 値は settings validation で fail-loud にする

    typo (linux/aarch64 等) を通すと build と Lambda アーキテクチャの不一致が
    起動時エラーまで発覚しない。
    """
    with pytest.raises(ValidationError, match="platform"):
        AwsContainer.model_validate(
            {
                "dockerfile_path": "Dockerfile",
                "platform": "linux/aarch64",
                "handlers": {
                    "wsgi": {"command": "pocket.django.lambda_handlers.wsgi_handler"}
                },
            }
        )
