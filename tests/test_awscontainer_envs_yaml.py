"""awscontainer.envs の CFn テンプレート埋め込みのテスト。

env 値はユーザー入力 (JSON 文字列等、二重引用符や特殊文字を含みうる) のため、
テンプレートへの埋め込みが YAML セーフであることを確認する。素の
`"{{ value }}"` 埋め込みだと値内の `"` で yaml.parser.ParserError になっていた
(回帰テスト)。
"""

from __future__ import annotations

import yaml
from moto import mock_aws
from pocket_cli.resources.aws.cloudformation import ContainerStack

from pocket.context import Context


def _write_envs_toml(tmp_path, envs_body: str):
    toml_path = tmp_path / "pocket.toml"
    toml_path.write_text(
        f"""
[general]
region = "ap-southeast-1"
project_name = "testprj"
stages = ["dev"]

[awscontainer]
dockerfile_path = "Dockerfile"

[awscontainer.handlers.wsgi]
command = "pocket.django.lambda_handlers.wsgi_handler"

[dev.awscontainer.envs]
{envs_body}
"""
    )
    return toml_path


def _lambda_env_vars(yaml_str: str) -> dict:
    parsed = yaml.safe_load(yaml_str)
    return parsed["Resources"]["WsgiLambdaFunction"]["Properties"]["Environment"][
        "Variables"
    ]


@mock_aws
def test_envs_with_double_quotes_render_yaml_safe(use_toml, tmp_path):
    """JSON 文字列 (二重引用符を含む値) が ParserError にならず round-trip すること"""
    json_value = '{"1":"arn:aws:cloudfront::123456789012:key-value-store/abc"}'
    toml_path = _write_envs_toml(tmp_path, "BLOCKLIST_KVS_ARNS = '%s'" % json_value)
    use_toml(str(toml_path))
    context = Context.from_toml(stage="dev")
    assert context.awscontainer
    variables = _lambda_env_vars(ContainerStack(context.awscontainer).yaml)
    assert variables["BLOCKLIST_KVS_ARNS"] == json_value


@mock_aws
def test_envs_plain_value_unchanged(use_toml, tmp_path):
    """通常の値 (引用符なし) も従来どおり文字列として埋め込まれること"""
    toml_path = _write_envs_toml(tmp_path, 'DJANGO_ENV_PATH = "project/env/env.dev"')
    use_toml(str(toml_path))
    context = Context.from_toml(stage="dev")
    assert context.awscontainer
    variables = _lambda_env_vars(ContainerStack(context.awscontainer).yaml)
    assert variables["DJANGO_ENV_PATH"] == "project/env/env.dev"
    assert variables["POCKET_STAGE"] == "dev"


@mock_aws
def test_envs_backslash_and_newline_render_yaml_safe(use_toml, tmp_path):
    """バックスラッシュ・改行を含む値も YAML セーフに round-trip すること"""
    toml_path = _write_envs_toml(tmp_path, r"""TRICKY = 'line1\nwith "quote" \\ end'""")
    use_toml(str(toml_path))
    context = Context.from_toml(stage="dev")
    assert context.awscontainer
    variables = _lambda_env_vars(ContainerStack(context.awscontainer).yaml)
    assert variables["TRICKY"] == 'line1\\nwith "quote" \\\\ end'
