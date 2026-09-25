"""pocket が作る IAM role (pocket_cli/resources/aws/iam_roles.py) のテスト。"""

import json

import boto3
import yaml as pyyaml
from moto import mock_aws
from pocket_cli.resources.aws import iam_roles

from pocket import settings
from pocket.context import ContainerContext, Context, DsqlContext

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
    # 既存 stack と同じ信頼ポリシーを保つ (0.38.0 以前: LambdaRole は Version なし、
    # scheduler は Version あり)。文書が変わると CFn が deploy 権限に無い
    # iam:UpdateAssumeRolePolicy を呼び、rollback ごと失敗する
    assert resources["LambdaRole"]["Properties"]["AssumeRolePolicyDocument"] == {
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ]
    }
    scheduler_trust = resources["SchedulerExecutionRole"]["Properties"][
        "AssumeRolePolicyDocument"
    ]
    assert scheduler_trust["Version"] == "2012-10-17"


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


def _rds_context(use_toml):
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.rds
    return context.container["main"], context.rds


def _statements(spec: iam_roles.RoleSpec, policy_suffix: str) -> list[dict]:
    names = [n for n in spec.inline_policies if n.endswith(policy_suffix)]
    assert len(names) == 1, spec.inline_policies.keys()
    return spec.inline_policies[names[0]]["Statement"]


def test_lambda_role_rds_aws_managed_uses_cluster_tag(use_toml):
    """RDS が作る secret は乱数入り ARN のため、元 cluster の aws: タグで絞る。"""
    container, rds = _rds_context(use_toml)
    spec = iam_roles.lambda_role(container, rds=rds)

    [statement] = _statements(spec, "access-rds-secret")
    assert statement["Action"] == ["secretsmanager:GetSecretValue"]
    assert statement["Resource"] == [
        {
            "Fn::Sub": "arn:${AWS::Partition}:secretsmanager:ap-northeast-1"
            ":${AWS::AccountId}:secret:rds!cluster-*"
        }
    ]
    assert statement["Condition"] == {
        "StringEquals": {
            "aws:ResourceTag/aws:rds:primaryDBClusterArn": {
                "Fn::Sub": "arn:${AWS::Partition}:rds:ap-northeast-1"
                f":${{AWS::AccountId}}:cluster:{rds.cluster_identifier}"
            }
        }
    }
    # 既定の aws/secretsmanager key で暗号化されるため KMS の付与は不要
    assert "kms" not in json.dumps(spec.inline_policies)


def test_lambda_role_rds_static_sm_uses_secret_name(use_toml):
    container, rds = _rds_context(use_toml)
    rds.password_strategy = "static"
    spec = iam_roles.lambda_role(container, rds=rds)

    [statement] = _statements(spec, "access-rds-secret")
    assert statement["Resource"] == [
        {
            "Fn::Sub": "arn:${AWS::Partition}:secretsmanager:ap-northeast-1"
            f":${{AWS::AccountId}}:secret:{rds.credentials_secret_name}-??????"
        }
    ]
    assert "Condition" not in statement


def test_lambda_role_rds_static_ssm_uses_parameter(use_toml):
    container, rds = _rds_context(use_toml)
    rds.password_strategy = "static"
    rds.secret_store = "ssm"
    spec = iam_roles.lambda_role(container, rds=rds)

    [statement] = _statements(spec, "access-rds-ssm")
    assert statement["Action"] == ["ssm:GetParameter"]
    assert not [n for n in spec.inline_policies if n.endswith("access-rds-secret")]


def test_lambda_role_rds_external_uses_configured_arn(use_toml):
    container, rds = _rds_context(use_toml)
    rds.managed = False
    rds.secret_arn = "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:x"
    spec = iam_roles.lambda_role(container, rds=rds)

    [statement] = _statements(spec, "access-rds-secret")
    assert statement["Resource"] == [rds.secret_arn]


def test_lambda_role_dsql_uses_name_tag(base_settings):
    """DSQL cluster の ARN も乱数入りのため、pocket が付ける Name タグで絞る。"""
    base_settings.dsql = settings.Dsql()
    base_settings.container["main"] = settings.Container(dockerfile_path="Dockerfile")
    dsql = DsqlContext.from_settings(base_settings.dsql, base_settings)
    container = ContainerContext.from_settings(
        "main", base_settings.container["main"], base_settings
    )
    spec = iam_roles.lambda_role(container, dsql=dsql)

    [statement] = _statements(spec, "access-dsql")
    assert statement["Action"] == ["dsql:DbConnectAdmin"]
    assert statement["Resource"] == [
        {
            "Fn::Sub": f"arn:${{AWS::Partition}}:dsql:{dsql.region}"
            ":${AWS::AccountId}:cluster/*"
        }
    ]
    assert statement["Condition"] == {
        "StringEquals": {"aws:ResourceTag/Name": dsql.tag_name}
    }
