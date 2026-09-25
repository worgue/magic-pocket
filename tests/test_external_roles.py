"""[iam] external_roles (pocket が IAM role を作らず既存 role を参照する) のテスト。"""

import json
from pathlib import Path

import boto3
import pytest
import yaml as pyyaml
from click.testing import CliRunner
from moto import mock_aws
from pocket_cli.cli.permissions_cli import permissions
from pocket_cli.resources.aws import iam_roles
from pocket_cli.resources.aws.cloudformation import ContainerStack
from pocket_cli.resources.aws.stage_roles import stage_role_specs, verify_stage_roles

from pocket import settings
from pocket.context import Context
from pocket.settings import Settings

REGION = "ap-southeast-1"
ACCOUNT_ID = "123456789012"


def _context(use_toml, *, external: bool = True) -> Context:
    use_toml("tests/data/toml/scheduler.toml")
    root = Settings.from_toml(stage="prod")
    root.iam.external_roles = external
    return Context.from_settings(root)


def _stack(context: Context) -> ContainerStack:
    return ContainerStack(
        context.container["main"], scheduler_context=context.scheduler["main"]
    )


def test_external_role_names_differ_from_managed(use_toml):
    """CFn が持つ既存 role と衝突しないよう、external では別名になる。"""
    managed = _stack(_context(use_toml, external=False))
    external = _stack(_context(use_toml))
    assert [r.name for r in external.role_specs] == [
        "prod-testprj-pocket-main-lambda-role",
        "prod-testprj-pocket-main-scheduler-role",
    ]
    managed_names = {r.name for r in managed.role_specs}
    assert managed_names.isdisjoint(r.name for r in external.role_specs)
    assert all(r.external for r in external.role_specs)
    assert not any(r.external for r in managed.role_specs)


@mock_aws
def test_template_references_external_roles_by_arn(monkeypatch, use_toml):
    """external では AWS::IAM::Role を出さず、既存 role を ARN で参照する。"""
    monkeypatch.delenv("POCKET_PERMISSIONS_BOUNDARY_ARN", raising=False)
    resources = pyyaml.safe_load(_stack(_context(use_toml)).yaml)["Resources"]

    assert "LambdaRole" not in resources
    assert "SchedulerExecutionRole" not in resources
    assert not [r for r in resources.values() if r["Type"] == "AWS::IAM::Role"]
    functions = [r for r in resources.values() if r["Type"] == "AWS::Lambda::Function"]
    assert functions
    for function in functions:
        assert function["Properties"]["Role"] == {
            "Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}"
            ":role/prod-testprj-pocket-main-lambda-role"
        }
    schedules = [
        r for r in resources.values() if r["Type"] == "AWS::Scheduler::Schedule"
    ]
    assert schedules
    for schedule in schedules:
        assert "SchedulerExecutionRole" not in schedule.get("DependsOn", [])
        assert schedule["Properties"]["Target"]["RoleArn"] == {
            "Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}"
            ":role/prod-testprj-pocket-main-scheduler-role"
        }


def test_policies_resolve_without_aws(use_toml):
    """role の policy は pocket.toml と account / region だけで決まる。"""
    for spec in _stack(_context(use_toml)).role_specs:
        resolved = spec.resolved_inline_policies(region=REGION, account_id=ACCOUNT_ID)
        text = json.dumps(resolved)
        assert "Fn::" not in text
        assert "${" not in text


def test_resolve_intrinsics_rejects_unknown_variable():
    with pytest.raises(ValueError, match="AWS::StackName"):
        iam_roles.resolve_intrinsics(
            {"Fn::Sub": "${AWS::StackName}"}, region=REGION, account_id=ACCOUNT_ID
        )


def _create_role(iam, spec: iam_roles.RoleSpec, *, skip_inline: str | None = None):
    """`pocket permissions roles` の出力どおりに role を作る (host 側の作業の模擬)。"""
    iam.create_role(
        RoleName=spec.name,
        AssumeRolePolicyDocument=json.dumps(spec.assume_role_policy),
    )
    for arn in spec.managed_policy_arns:
        iam.attach_role_policy(RoleName=spec.name, PolicyArn=arn)
    policies = spec.resolved_inline_policies(region=REGION, account_id=ACCOUNT_ID)
    for name, document in policies.items():
        if name != skip_inline:
            iam.put_role_policy(
                RoleName=spec.name,
                PolicyName=name,
                PolicyDocument=json.dumps(document),
            )


def _custom_spec(iam) -> iam_roles.RoleSpec:
    """moto は既定で AWS managed policy を持たないため customer managed を使う。"""
    document = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "*"}
            ],
        }
    )
    arn = iam.create_policy(PolicyName="managed-a", PolicyDocument=document)["Policy"][
        "Arn"
    ]
    return iam_roles.RoleSpec(
        name="prod-testprj-pocket-main-lambda-role",
        service="lambda.amazonaws.com",
        managed_policy_arns=[arn],
        inline_policies={
            "access": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["ssm:GetParameter"],
                        "Resource": [
                            {
                                "Fn::Sub": "arn:aws:ssm:${AWS::Region}"
                                ":${AWS::AccountId}:parameter/x"
                            }
                        ],
                    }
                ],
            }
        },
        external=True,
    )


@mock_aws
def test_verify_role_accepts_matching_role_with_extra_policies():
    iam = boto3.client("iam", region_name=REGION)
    spec = _custom_spec(iam)
    _create_role(iam, spec)
    # 利用者が足した policy は許容する
    iam.put_role_policy(
        RoleName=spec.name,
        PolicyName="extra",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"}],
            }
        ),
    )
    arn = iam_roles.verify_role(iam, spec, region=REGION)
    assert arn == f"arn:aws:iam::{ACCOUNT_ID}:role/{spec.name}"


@mock_aws
def test_verify_role_reports_missing_role():
    iam = boto3.client("iam", region_name=REGION)
    spec = _custom_spec(iam)
    with pytest.raises(iam_roles.RoleMismatchError, match="がありません"):
        iam_roles.verify_role(iam, spec, region=REGION)


@mock_aws
def test_verify_role_reports_each_problem():
    iam = boto3.client("iam", region_name=REGION)
    spec = _custom_spec(iam)
    _create_role(iam, spec, skip_inline="access")
    iam.detach_role_policy(RoleName=spec.name, PolicyArn=spec.managed_policy_arns[0])
    iam.update_assume_role_policy(
        RoleName=spec.name,
        PolicyDocument=json.dumps(
            iam_roles.RoleSpec(name="x", service="ec2.amazonaws.com").assume_role_policy
        ),
    )
    with pytest.raises(iam_roles.RoleMismatchError) as e:
        iam_roles.verify_role(iam, spec, region=REGION)
    message = str(e.value)
    assert "lambda.amazonaws.com を許可していません" in message
    assert "managed policy" in message
    assert "inline policy access がありません" in message


@mock_aws
def test_verify_role_detects_changed_inline_policy():
    iam = boto3.client("iam", region_name=REGION)
    spec = _custom_spec(iam)
    _create_role(iam, spec)
    iam.put_role_policy(
        RoleName=spec.name,
        PolicyName="access",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Action": "ssm:*", "Resource": "*"}],
            }
        ),
    )
    with pytest.raises(iam_roles.RoleMismatchError, match="access の内容が違います"):
        iam_roles.verify_role(iam, spec, region=REGION)


@mock_aws
def test_verify_role_ignores_equivalent_formatting():
    """要素 1 つの list を値にした形・並べ替えた形は同じ policy とみなす。"""
    iam = boto3.client("iam", region_name=REGION)
    spec = _custom_spec(iam)
    _create_role(iam, spec, skip_inline="access")
    iam.put_role_policy(
        RoleName=spec.name,
        PolicyName="access",
        PolicyDocument=json.dumps(
            {
                "Statement": [
                    {
                        "Resource": f"arn:aws:ssm:{REGION}:{ACCOUNT_ID}:parameter/x",
                        "Action": "ssm:GetParameter",
                        "Effect": "Allow",
                    }
                ],
                "Version": "2012-10-17",
            }
        ),
    )
    iam_roles.verify_role(iam, spec, region=REGION)


@mock_aws
def test_ensure_role_does_not_create_external_role():
    iam = boto3.client("iam", region_name=REGION)
    spec = iam_roles.backup_role(
        "prod-testprj-pocket-backup-role", permissions_boundary=None, external=True
    )
    with pytest.raises(iam_roles.RoleMismatchError):
        iam_roles.ensure_role(iam, spec)
    assert iam.list_roles()["Roles"] == []


@mock_aws
def test_permissions_roles_output_matches_verification(monkeypatch, use_toml, tmp_path):
    """`pocket permissions roles` の出力どおりに作れば deploy 前の検査が通る。"""
    monkeypatch.delenv("POCKET_PERMISSIONS_BOUNDARY_ARN", raising=False)
    toml = tmp_path / "pocket.toml"
    toml.write_text(
        Path("tests/data/toml/scheduler.toml").read_text()
        + "\n[iam]\nexternal_roles = true\n"
    )
    use_toml(str(toml))
    result = CliRunner().invoke(permissions, ["roles", "--stage", "prod"])
    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    assert output["external_roles"] is True
    assert [r["name"] for r in output["roles"]] == [
        "prod-testprj-pocket-main-lambda-role",
        "prod-testprj-pocket-main-scheduler-role",
        "prod-testprj-pocket-codebuild-role",
    ]

    context = Context.from_toml(stage="prod")
    with pytest.raises(iam_roles.RoleMismatchError):
        verify_stage_roles(context)
    iam = boto3.client("iam", region_name=REGION)
    for role in output["roles"]:
        iam.create_role(
            RoleName=role["name"],
            AssumeRolePolicyDocument=json.dumps(role["assume_role_policy"]),
        )
        for name, document in role["inline_policies"].items():
            iam.put_role_policy(
                RoleName=role["name"],
                PolicyName=name,
                PolicyDocument=json.dumps(document),
            )
    # moto は AWS managed policy を持たないため、managed policy は検査対象から外す
    monkeypatch.setattr(
        iam_roles, "_managed_policy_problems", lambda iam_client, spec: []
    )
    verify_stage_roles(context)


def test_verify_stage_roles_skips_managed_mode(use_toml):
    """external_roles でなければ AWS に問い合わせない (mock なしで通る)。"""
    verify_stage_roles(_context(use_toml, external=False))


def test_stage_role_specs_includes_backup_role_for_dsql(base_settings):
    base_settings.dsql = settings.Dsql()
    base_settings.iam.external_roles = True
    context = Context.from_settings(base_settings)
    [spec] = stage_role_specs(context, account_id=ACCOUNT_ID)
    assert spec.name == "test-testprj-pocket-backup-role"
    assert spec.service == "backup.amazonaws.com"
    assert spec.external
