"""handler 単位の envs 上書きのテスト。

同一イメージを環境変数でモード切替して複数 Lambda に並べる用途
(handlers.<name>.envs)。container 共通 [awscontainer].envs とマージされ
handler 側が優先されること、他 handler に漏れないことを確認する。
"""

from __future__ import annotations

import yaml
from moto import mock_aws
from pocket_cli.resources.aws.cloudformation import ContainerStack

from pocket.context import Context


def _write_toml(tmp_path):
    toml_path = tmp_path / "pocket.toml"
    toml_path.write_text(
        """
[general]
region = "ap-southeast-1"
project_name = "testprj"
stages = ["dev"]

[awscontainer]
dockerfile_path = "Dockerfile"

[awscontainer.envs]
SHARED = "container"
OVERRIDE_ME = "container"

[awscontainer.handlers.web]
command = "myapp-lambda"

[awscontainer.handlers.admin]
command = "myapp-lambda"
timeout = 600
envs = { MYAPP_MODE = "admin", OVERRIDE_ME = "handler" }
"""
    )
    return toml_path


def _handler_env_vars(yaml_str: str, logical_name: str) -> dict:
    parsed = yaml.safe_load(yaml_str)
    return parsed["Resources"][logical_name]["Properties"]["Environment"]["Variables"]


@mock_aws
def test_handler_envs_merge_and_override(use_toml, tmp_path):
    """handler.envs が container envs にマージされ handler 側が優先されること"""
    use_toml(str(_write_toml(tmp_path)))
    context = Context.from_toml(stage="dev")
    assert context.awscontainer
    yaml_str = ContainerStack(context.awscontainer).yaml
    admin = _handler_env_vars(yaml_str, "AdminLambdaFunction")
    assert admin["MYAPP_MODE"] == "admin"
    assert admin["OVERRIDE_ME"] == "handler"
    assert admin["SHARED"] == "container"


@mock_aws
def test_handler_envs_do_not_leak_to_other_handlers(use_toml, tmp_path):
    """handler.envs が他の handler に混入しないこと"""
    use_toml(str(_write_toml(tmp_path)))
    context = Context.from_toml(stage="dev")
    assert context.awscontainer
    yaml_str = ContainerStack(context.awscontainer).yaml
    web = _handler_env_vars(yaml_str, "WebLambdaFunction")
    assert "MYAPP_MODE" not in web
    assert web["OVERRIDE_ME"] == "container"
    assert web["SHARED"] == "container"


@mock_aws
def test_override_key_not_duplicated_in_yaml(use_toml, tmp_path):
    """上書きキーが YAML mapping に二重出力されないこと

    素朴な「共通 envs → handler envs の順で全部出す」実装だと同一キーが 2 回
    現れる (yaml.safe_load は後勝ちで隠れるが、CFn の挙動に依存する脆い形になる)。
    テンプレート出力自体に重複がないことをテキストで確認する。
    """
    use_toml(str(_write_toml(tmp_path)))
    context = Context.from_toml(stage="dev")
    assert context.awscontainer
    yaml_str = ContainerStack(context.awscontainer).yaml
    assert yaml_str.count('"OVERRIDE_ME"') == 2  # web と admin で 1 回ずつ
