"""POCKET_HOSTS のカンマ区切り生成のテスト。

POCKET_HOSTS は pocket/django/runtime.py の add_or_append_env でカンマ結合の
ALLOWED_HOSTS に append される。生成側がセパレータなしで join していた
バグの回帰テスト (apigateway 付き handler が 2 つ以上で発火していた)。
"""

import os

from moto import mock_aws

from pocket import runtime


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

[awscontainer.handlers.wsgi]
command = "pocket.django.lambda_handlers.wsgi_handler"
"""
    )
    return toml_path


@mock_aws
def test_pocket_hosts_is_comma_separated(use_toml, tmp_path, monkeypatch):
    """複数 handler の host がカンマ区切りで POCKET_HOSTS に入る。"""
    use_toml(str(_write_toml(tmp_path)))
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(
        runtime,
        "_get_hosts",
        lambda ac: {"wsgi": "a.example.com", "admin": "b.example.com"},
    )
    monkeypatch.setattr(runtime, "_get_queueurls", lambda ac: {})

    runtime.set_envs_from_aws_resources(stage="dev")
    assert os.environ["POCKET_HOSTS"] == "a.example.com,b.example.com"


def test_add_or_append_env_joins_with_comma(monkeypatch):
    """ALLOWED_HOSTS への append はカンマ結合 (POCKET_HOSTS の形式と整合)。"""
    from pocket.django.runtime import add_or_append_env

    monkeypatch.setattr(os, "environ", {})
    add_or_append_env("ALLOWED_HOSTS", "a.example.com,b.example.com")
    assert os.environ["ALLOWED_HOSTS"] == "a.example.com,b.example.com"
    add_or_append_env("ALLOWED_HOSTS", "c.example.com")
    assert os.environ["ALLOWED_HOSTS"] == "a.example.com,b.example.com,c.example.com"
