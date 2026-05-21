"""[awscontainer.iam] section のテスト。

ユーザー提供の managed_policy_arns / inline_policies が Lambda execution role
に注入されることを確認する。
"""

from moto import mock_aws

from pocket.context import Context


def _write_iam_toml(tmp_path, body: str):
    """awscontainer.iam を含む最小 pocket.toml を tmp_path に書き、パスを返す。"""
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

{body}
"""
    )
    return toml_path


@mock_aws
def test_iam_default_is_empty(use_toml, tmp_path):
    """[awscontainer.iam] 未指定なら managed_policy_arns / inline_policies が空。"""
    toml_path = _write_iam_toml(tmp_path, "")
    use_toml(str(toml_path))
    context = Context.from_toml(stage="dev")
    assert context.awscontainer
    assert context.awscontainer.iam.managed_policy_arns == []
    assert context.awscontainer.iam.inline_policies == {}


@mock_aws
def test_iam_managed_policy_arns_in_yaml(use_toml, tmp_path):
    """managed_policy_arns が LambdaRole の ManagedPolicyArns に追加されること。"""
    arn = "arn:aws:iam::aws:policy/AdministratorAccess"
    toml_path = _write_iam_toml(
        tmp_path,
        f"""
[awscontainer.iam]
managed_policy_arns = [
    "{arn}",
]
""",
    )
    use_toml(str(toml_path))
    context = Context.from_toml(stage="dev")
    from pocket_cli.resources.aws.cloudformation import ContainerStack

    assert context.awscontainer
    stack = ContainerStack(context.awscontainer)
    yaml = stack.yaml

    assert arn in yaml
    # built-in な BasicExecutionRole も並んで存在することを確認
    assert "AWSLambdaBasicExecutionRole" in yaml


@mock_aws
def test_iam_inline_policies_in_yaml(use_toml, tmp_path):
    """inline_policies が LambdaRole の Policies に追加されること。"""
    toml_path = _write_iam_toml(
        tmp_path,
        """
[awscontainer.iam.inline_policies.cross-account-assume]
Version = "2012-10-17"

[[awscontainer.iam.inline_policies.cross-account-assume.Statement]]
Effect = "Allow"
Action = "sts:AssumeRole"
Resource = "arn:aws:iam::*:role/external-provisioner-role"
""",
    )
    use_toml(str(toml_path))
    context = Context.from_toml(stage="dev")
    from pocket_cli.resources.aws.cloudformation import ContainerStack

    assert context.awscontainer
    stack = ContainerStack(context.awscontainer)
    yaml = stack.yaml

    # PolicyName は resource_prefix が前置される
    assert "cross-account-assume" in yaml
    assert "sts:AssumeRole" in yaml
    assert "arn:aws:iam::*:role/external-provisioner-role" in yaml


@mock_aws
def test_iam_multiple_arns_and_inline_policies(use_toml, tmp_path):
    """複数の managed_policy_arns と inline_policies が全て YAML に出力されること。"""
    toml_path = _write_iam_toml(
        tmp_path,
        """
[awscontainer.iam]
managed_policy_arns = [
    "arn:aws:iam::aws:policy/AdministratorAccess",
    "arn:aws:iam::aws:policy/IAMReadOnlyAccess",
]

[awscontainer.iam.inline_policies.organizations-read]
Version = "2012-10-17"

[[awscontainer.iam.inline_policies.organizations-read.Statement]]
Effect = "Allow"
Action = ["organizations:ListAccounts", "organizations:DescribeAccount"]
Resource = "*"

[awscontainer.iam.inline_policies.ssm-write]
Version = "2012-10-17"

[[awscontainer.iam.inline_policies.ssm-write.Statement]]
Effect = "Allow"
Action = ["ssm:PutParameter", "ssm:DeleteParameter"]
Resource = "arn:aws:ssm:*:*:parameter/external-tool/*"
""",
    )
    use_toml(str(toml_path))
    context = Context.from_toml(stage="dev")
    from pocket_cli.resources.aws.cloudformation import ContainerStack

    assert context.awscontainer
    stack = ContainerStack(context.awscontainer)
    yaml = stack.yaml

    assert "AdministratorAccess" in yaml
    assert "IAMReadOnlyAccess" in yaml
    assert "organizations-read" in yaml
    assert "organizations:ListAccounts" in yaml
    assert "ssm-write" in yaml
    assert "ssm:PutParameter" in yaml
