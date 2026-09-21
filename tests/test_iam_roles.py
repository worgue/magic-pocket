"""pocket が作る IAM role (pocket_cli/resources/aws/iam_roles.py) のテスト。"""

import json

import boto3
import yaml as pyyaml
from moto import mock_aws
from pocket_cli.resources.aws import iam_roles

from pocket.context import Context

REGION = "ap-southeast-1"
BOUNDARY = "arn:aws:iam::123456789012:policy/test-boundary"


def _codebuild_spec(boundary: str | None = None) -> iam_roles.RoleSpec:
    return iam_roles.codebuild_role(
        name="dev-testprj-pocket-codebuild-role",
        region=REGION,
        account_id="123456789012",
        state_bucket="state-bucket",
        project_name="dev-testprj-pocket-codebuild",
        permissions_boundary=boundary,
    )


def _managed_spec(iam) -> iam_roles.RoleSpec:
    """managed policy 2 つを持つ spec (moto は既定で AWS managed policy を持たない
    ため、customer managed policy を作って使う)。"""
    document = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "*"}
            ],
        }
    )
    arns = [
        iam.create_policy(PolicyName=name, PolicyDocument=document)["Policy"]["Arn"]
        for name in ("managed-a", "managed-b")
    ]
    return iam_roles.RoleSpec(
        name="managed-role", service="backup.amazonaws.com", managed_policy_arns=arns
    )


def test_cfn_properties_omit_empty_fields():
    """managed / inline / boundary が無ければ Properties にキー自体を出さない。"""
    spec = iam_roles.RoleSpec(name="r", service="lambda.amazonaws.com")
    assert spec.cfn_properties == {
        "RoleName": "r",
        "AssumeRolePolicyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "lambda.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        },
    }


@mock_aws
def test_container_stack_embeds_lambda_role_spec(monkeypatch, use_toml):
    """テンプレートの LambdaRole は lambda_role() の Properties そのもの。"""
    monkeypatch.delenv("POCKET_PERMISSIONS_BOUNDARY_ARN", raising=False)
    use_toml("tests/data/toml/scheduler.toml")
    context = Context.from_toml(stage="prod")
    from pocket_cli.resources.aws.cloudformation import ContainerStack

    container = context.container["main"]
    assert container
    stack = ContainerStack(container, scheduler_context=context.scheduler["main"])
    resources = pyyaml.safe_load(stack.yaml)["Resources"]

    assert resources["LambdaRole"]["Properties"] == (
        iam_roles.lambda_role(container).cfn_properties
    )
    assert resources["SchedulerExecutionRole"]["Properties"] == (
        iam_roles.scheduler_role(
            context.scheduler["main"],
            permissions_boundary=container.permissions_boundary,
        ).cfn_properties
    )


@mock_aws
def test_ensure_role_creates_with_policies_and_boundary(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    iam = boto3.client("iam", region_name=REGION)
    spec = _codebuild_spec(BOUNDARY)

    arn = iam_roles.ensure_role(iam, spec)

    role = iam.get_role(RoleName=spec.name)["Role"]
    assert role["Arn"] == arn
    assert role["PermissionsBoundary"]["PermissionsBoundaryArn"] == BOUNDARY
    assert role["AssumeRolePolicyDocument"] == spec.assume_role_policy
    doc = iam.get_role_policy(RoleName=spec.name, PolicyName="codebuild-policy")
    assert doc["PolicyDocument"] == json.loads(
        json.dumps(spec.inline_policies["codebuild-policy"])
    )


@mock_aws
def test_ensure_role_returns_existing_without_changes(monkeypatch):
    """既存 role は ARN を返すだけ (policy の付け直しも伝播待ちもしない)。"""
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    iam = boto3.client("iam", region_name=REGION)
    spec = _managed_spec(iam)
    first = iam_roles.ensure_role(iam, spec)
    iam.detach_role_policy(RoleName=spec.name, PolicyArn=spec.managed_policy_arns[0])

    assert iam_roles.ensure_role(iam, spec) == first
    assert len(sleeps) == 1
    attached = iam.list_attached_role_policies(RoleName=spec.name)
    assert len(attached["AttachedPolicies"]) == 1


@mock_aws
def test_delete_role_removes_managed_and_inline_policies(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    iam = boto3.client("iam", region_name=REGION)
    managed = _managed_spec(iam)
    codebuild = _codebuild_spec()
    iam_roles.ensure_role(iam, managed)
    iam_roles.ensure_role(iam, codebuild)

    assert iam_roles.delete_role(iam, managed.name, managed.managed_policy_arns)
    assert iam_roles.delete_role(iam, codebuild.name)
    assert iam.list_roles()["Roles"] == []


@mock_aws
def test_delete_role_returns_false_when_absent():
    iam = boto3.client("iam", region_name=REGION)
    assert (
        iam_roles.delete_role(iam, "missing-role", iam_roles.BACKUP_ROLE_POLICIES)
        is False
    )
