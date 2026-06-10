"""[awscontainer].ecr_name の上書きテスト。

省略時は resource_prefix + "lambda" を導出し、指定時はその値で上書きされることを
確認する。同一 AWS アカウント内で複数 stage が同じ ECR repo を共有し、build once +
commit-hash 昇格 (再ビルドなし deploy) を成立させるための設定 (Step B)。
"""

from moto import mock_aws

from pocket.context import Context


def _write_toml(tmp_path, awscontainer_body: str):
    """awscontainer を含む最小 pocket.toml を tmp_path に書き、パスを返す。"""
    toml_path = tmp_path / "pocket.toml"
    toml_path.write_text(
        f"""
[general]
region = "ap-southeast-1"
project_name = "testprj"
stages = ["dev"]

[awscontainer]
dockerfile_path = "Dockerfile"
{awscontainer_body}

[awscontainer.handlers.wsgi]
command = "pocket.django.lambda_handlers.wsgi_handler"
"""
    )
    return toml_path


@mock_aws
def test_ecr_name_defaults_to_resource_prefix(use_toml, tmp_path):
    """ecr_name 未指定なら resource_prefix + "lambda" が導出される。"""
    use_toml(str(_write_toml(tmp_path, "")))
    context = Context.from_toml(stage="dev")
    assert context.awscontainer
    # prefix_template デフォルト "{stage}-{project}-{namespace}-" + "lambda"
    assert context.awscontainer.ecr_name == "dev-testprj-pocket-lambda"
    assert context.awscontainer.ecr_name_overridden is False


@mock_aws
def test_ecr_name_override(use_toml, tmp_path):
    """[awscontainer].ecr_name 指定時はその値で上書きされる。"""
    use_toml(str(_write_toml(tmp_path, 'ecr_name = "shared-repo"')))
    context = Context.from_toml(stage="dev")
    assert context.awscontainer
    assert context.awscontainer.ecr_name == "shared-repo"
    assert context.awscontainer.ecr_name_overridden is True


def _setup_destroy(context, monkeypatch):
    """destroy テスト用に stack/codebuild/logs/vpc の副作用を無効化する。"""
    from pocket_cli.resources.aws.cloudformation import ContainerStack

    monkeypatch.setattr("pocket_cli.cli.destroy_cli._destroy_codebuild", lambda c: None)
    monkeypatch.setattr(
        "pocket_cli.cli.destroy_cli._destroy_log_groups", lambda c: None
    )
    monkeypatch.setattr("pocket_cli.cli.destroy_cli._destroy_vpc", lambda c: None)
    monkeypatch.setattr(ContainerStack, "status", property(lambda self: "NOEXIST"))


@mock_aws
def test_destroy_deletes_default_ecr(use_toml, tmp_path, monkeypatch):
    """ecr_name 未指定 (導出名) なら destroy で ECR repo が削除される (従来挙動)。"""
    import boto3
    from pocket_cli.cli.destroy_cli import _destroy_awscontainer

    use_toml(str(_write_toml(tmp_path, "")))
    context = Context.from_toml(stage="dev")
    client = boto3.client("ecr", region_name="ap-southeast-1")
    client.create_repository(repositoryName="dev-testprj-pocket-lambda")
    _setup_destroy(context, monkeypatch)

    _destroy_awscontainer(context, with_secrets=False)
    assert client.describe_repositories()["repositories"] == []


@mock_aws
def test_destroy_skips_overridden_ecr(use_toml, tmp_path, monkeypatch):
    """ecr_name 明示指定時は他 stage と共有の可能性があるため destroy で削除しない。"""
    import boto3
    from pocket_cli.cli.destroy_cli import _destroy_awscontainer

    use_toml(str(_write_toml(tmp_path, 'ecr_name = "shared-repo"')))
    context = Context.from_toml(stage="dev")
    client = boto3.client("ecr", region_name="ap-southeast-1")
    client.create_repository(repositoryName="shared-repo")
    _setup_destroy(context, monkeypatch)

    _destroy_awscontainer(context, with_secrets=False)
    names = [
        r["repositoryName"] for r in client.describe_repositories()["repositories"]
    ]
    assert names == ["shared-repo"]
