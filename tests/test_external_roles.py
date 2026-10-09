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


def _create_role(iam, spec: iam_roles.RoleSpec, *, service: str | None = None):
    """role の持ち主が自分の流儀で作った role の模擬。policy は pocket の出力と
    無関係 (組織の標準セット)。信頼先だけが pocket の求めるものと一致する。"""
    trust = iam_roles.RoleSpec(name="x", service=service or spec.service)
    iam.create_role(
        RoleName=spec.name,
        AssumeRolePolicyDocument=json.dumps(trust.assume_role_policy),
    )
    iam.put_role_policy(
        RoleName=spec.name,
        PolicyName="org-standard",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"}],
            }
        ),
    )


def _spec() -> iam_roles.RoleSpec:
    return iam_roles.RoleSpec(
        name="prod-testprj-pocket-main-lambda-role",
        service="lambda.amazonaws.com",
        managed_policy_arns=["arn:aws:iam::aws:policy/AmazonS3FullAccess"],
        inline_policies={
            "access": {
                "Version": "2012-10-17",
                "Statement": [
                    {"Effect": "Allow", "Action": ["ssm:GetParameter"], "Resource": "*"}
                ],
            }
        },
        external=True,
    )


@mock_aws
def test_verify_role_checks_existence_and_trust_only():
    """policy の中身は検査しない。pocket の出力と違う policy でも、存在して
    service を信頼していれば通る。"""
    iam = boto3.client("iam", region_name=REGION)
    spec = _spec()
    _create_role(iam, spec)
    arn = iam_roles.verify_role(iam, spec)
    assert arn == f"arn:aws:iam::{ACCOUNT_ID}:role/{spec.name}"


@mock_aws
def test_verify_role_reports_missing_role():
    iam = boto3.client("iam", region_name=REGION)
    with pytest.raises(iam_roles.RoleMismatchError, match="がありません"):
        iam_roles.verify_role(iam, _spec())


@mock_aws
def test_verify_role_reports_wrong_trust():
    iam = boto3.client("iam", region_name=REGION)
    spec = _spec()
    _create_role(iam, spec, service="ec2.amazonaws.com")
    with pytest.raises(
        iam_roles.RoleMismatchError,
        match="lambda.amazonaws.com に sts:AssumeRole を許可していません",
    ):
        iam_roles.verify_role(iam, spec)


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
    """`pocket permissions roles` の名前と信頼先で作れば deploy 前の検査が通る。"""
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
    # 信頼ポリシーだけ出力に合わせ、policy は付けない (持ち主の責任なので検査しない)
    iam = boto3.client("iam", region_name=REGION)
    for role in output["roles"]:
        iam.create_role(
            RoleName=role["name"],
            AssumeRolePolicyDocument=json.dumps(role["assume_role_policy"]),
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
