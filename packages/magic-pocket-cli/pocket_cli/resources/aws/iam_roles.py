"""pocket が作る IAM role の定義と、その作成・削除。

pocket が作る role はここに列挙した 4 種だけで、どれも RoleSpec (名前・信頼する
service・managed policy・inline policy・boundary) で表す。作成経路は 2 通り:

- CFn: Lambda 実行 role / scheduler role。container stack のテンプレートが
  RoleSpec.cfn_properties をそのまま埋め込む
- API: CodeBuild role / AWS Backup role。stack を持たないリソースから
  ensure_role / delete_role で作る

inline policy の Resource には CFn の Fn::Sub / Fn::GetAtt が入りうる
(CFn 経路の role のみ)。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError

from pocket.utils import echo

if TYPE_CHECKING:
    from mypy_boto3_iam import IAMClient

    from pocket.context import ContainerContext, SchedulerContext
    from pocket.inbound_context import InboundContext

# IAM ロールの伝播待ち (作成直後に使うと AssumeRole / PassRole が失敗する)
ROLE_PROPAGATION_WAIT_SECONDS = 10

_POLICY_VERSION = "2012-10-17"

BACKUP_ROLE_POLICIES = [
    "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup",
    "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForRestores",
]


@dataclass(frozen=True)
class RoleSpec:
    name: str
    service: str
    managed_policy_arns: list[str] = field(default_factory=list)
    # PolicyName -> PolicyDocument
    inline_policies: dict[str, dict[str, Any]] = field(default_factory=dict)
    permissions_boundary: str | None = None

    @property
    def assume_role_policy(self) -> dict[str, Any]:
        return {
            "Version": _POLICY_VERSION,
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": self.service},
                    "Action": "sts:AssumeRole",
                }
            ],
        }

    @property
    def cfn_properties(self) -> dict[str, Any]:
        """AWS::IAM::Role の Properties。"""
        props: dict[str, Any] = {
            "RoleName": self.name,
            "AssumeRolePolicyDocument": self.assume_role_policy,
        }
        if self.managed_policy_arns:
            props["ManagedPolicyArns"] = self.managed_policy_arns
        if self.inline_policies:
            props["Policies"] = [
                {"PolicyName": name, "PolicyDocument": doc}
                for name, doc in self.inline_policies.items()
            ]
        if self.permissions_boundary:
            props["PermissionsBoundary"] = self.permissions_boundary
        return props


def _policy(*statements: dict[str, Any]) -> dict[str, Any]:
    return {"Version": _POLICY_VERSION, "Statement": list(statements)}


def _allow(actions: list[str], resources: list[Any]) -> dict[str, Any]:
    return {"Effect": "Allow", "Action": actions, "Resource": resources}


def _sub(value: str) -> dict[str, str]:
    return {"Fn::Sub": value}


# --- CFn 経路 ---


def lambda_role(
    ctx: ContainerContext,
    *,
    rds_secret_arn: str | None = None,
    rds_kms_key_id: str | None = None,
    rds_ssm_param_arn: str | None = None,
    dsql_cluster_arn: str | None = None,
) -> RoleSpec:
    """container の Lambda 実行 role (全 handler 共通)。

    rds_* / dsql_cluster_arn は ContainerStack が deploy 時に AWS から解決した値。
    """
    prefix = ctx.resource_prefix
    policies: dict[str, dict[str, Any]] = {}
    for inlet in ctx.inbound.values():
        policies[f"inbound-{inlet.name}"] = _inbound_policy(inlet)
    policies.update(_secrets_policies(ctx))
    policies.update(
        _database_policies(
            prefix,
            rds_secret_arn=rds_secret_arn,
            rds_kms_key_id=rds_kms_key_id,
            rds_ssm_param_arn=rds_ssm_param_arn,
            dsql_cluster_arn=dsql_cluster_arn,
        )
    )
    policies[f"{prefix}access-cloudformation"] = _policy(
        _allow(
            ["cloudformation:DescribeStacks"],
            [
                _sub(
                    "arn:aws:cloudformation:${AWS::Region}:${AWS::AccountId}"
                    f":stack/{ctx.slug}-*"
                )
            ],
        )
    )
    for policy_name, doc in ctx.iam.inline_policies.items():
        policies[f"{prefix}{policy_name}"] = doc

    return RoleSpec(
        name=f"lambda-{ctx.slug}-{ctx.name}-{ctx.namespace}",
        service="lambda.amazonaws.com",
        managed_policy_arns=_lambda_managed_policies(ctx),
        inline_policies=policies,
        permissions_boundary=ctx.permissions_boundary,
    )


def _lambda_managed_policies(ctx: ContainerContext) -> list[str]:
    managed = ["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"]
    if ctx.vpc:
        managed.append(
            "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
        )
    for enabled, policy_name in (
        (ctx.use_ses, "AmazonSESFullAccess"),
        (ctx.use_s3, "AmazonS3FullAccess"),
        (ctx.use_route53, "AmazonRoute53FullAccess"),
        (ctx.use_sqs, "AmazonSQSFullAccess"),
        (ctx.use_efs, "AmazonElasticFileSystemFullAccess"),
    ):
        if enabled:
            managed.append(f"arn:aws:iam::aws:policy/{policy_name}")
    managed.extend(ctx.iam.managed_policy_arns)
    return managed


def _inbound_policy(inlet: InboundContext) -> dict[str, Any]:
    """受信口の bucket を読む権限。受信した原本と metadata は消させない。"""
    bucket = f"arn:${{AWS::Partition}}:s3:::{inlet.bucket_name}"
    raw = _sub(f"{bucket}/{inlet.config.raw_prefix}*")
    metadata = _sub(f"{bucket}/{inlet.config.metadata_prefix}*")
    imported = _sub(f"{bucket}/{inlet.config.import_prefix}*")
    return _policy(
        _allow(["s3:ListBucket"], [_sub(bucket)]),
        {
            "Effect": "Deny",
            "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"],
            "Resource": [raw, metadata],
        },
        _allow(["s3:GetObject", "s3:GetObjectVersion"], [raw, imported, metadata]),
        _allow(["s3:PutObject"], [metadata]),
    )


def _secrets_policies(ctx: ContainerContext) -> dict[str, dict[str, Any]]:
    # allowed_*_resources は container store + shared store の合算
    prefix = ctx.resource_prefix
    policies: dict[str, dict[str, Any]] = {}
    if ctx.allowed_sm_resources:
        statements = [
            _allow(
                ["secretsmanager:GetSecretValue"],
                [_sub(arn) for arn in ctx.allowed_sm_resources],
            )
        ]
        if ctx.require_list_secrets:
            statements.append(_allow(["secretsmanager:ListSecrets"], ["*"]))
        policies[f"{prefix}access-secretsmanager"] = _policy(*statements)
    if ctx.allowed_ssm_resources:
        policies[f"{prefix}access-ssm"] = _policy(
            _allow(
                [
                    "ssm:GetParameter",
                    "ssm:GetParameters",
                    "ssm:GetParametersByPath",
                ],
                [_sub(arn) for arn in ctx.allowed_ssm_resources],
            )
        )
    return policies


def _database_policies(
    prefix: str,
    *,
    rds_secret_arn: str | None,
    rds_kms_key_id: str | None,
    rds_ssm_param_arn: str | None,
    dsql_cluster_arn: str | None,
) -> dict[str, dict[str, Any]]:
    policies: dict[str, dict[str, Any]] = {}
    if rds_secret_arn:
        statements = [_allow(["secretsmanager:GetSecretValue"], [rds_secret_arn])]
        if rds_kms_key_id:
            statements.append(_allow(["kms:Decrypt"], [rds_kms_key_id]))
        policies[f"{prefix}access-rds-secret"] = _policy(*statements)
    if rds_ssm_param_arn:
        policies[f"{prefix}access-rds-ssm"] = _policy(
            _allow(["ssm:GetParameter"], [_sub(rds_ssm_param_arn)])
        )
    if dsql_cluster_arn:
        policies[f"{prefix}access-dsql"] = _policy(
            _allow(["dsql:DbConnectAdmin"], [dsql_cluster_arn])
        )
    return policies


def scheduler_role(
    scheduler: SchedulerContext, *, permissions_boundary: str | None
) -> RoleSpec:
    """EventBridge Scheduler が handler を起動する role (container 単位)。"""
    statements: list[dict[str, Any]] = []
    if scheduler.invoked_function_arns:
        statements.append(
            _allow(
                ["lambda:InvokeFunction"],
                [_sub(arn) for arn in scheduler.invoked_function_arns],
            )
        )
    if scheduler.sqs_queue_logical_names:
        statements.append(
            _allow(
                ["sqs:SendMessage"],
                [
                    {"Fn::GetAtt": f"{name}.Arn"}
                    for name in scheduler.sqs_queue_logical_names
                ],
            )
        )
    return RoleSpec(
        name=scheduler.role_name,
        service="scheduler.amazonaws.com",
        inline_policies={"invoke-handlers": _policy(*statements)},
        # boundary 強制 account (iam:CreateRole が boundary 付きのみ許可) では
        # Lambda role と同様に boundary が無いと作成できない
        permissions_boundary=permissions_boundary,
    )


# --- API 経路 ---


def codebuild_role(
    *,
    name: str,
    region: str,
    account_id: str,
    state_bucket: str,
    project_name: str,
    permissions_boundary: str | None,
) -> RoleSpec:
    """CodeBuild で image を build して ECR へ push する role。"""
    return RoleSpec(
        name=name,
        service="codebuild.amazonaws.com",
        inline_policies={
            "codebuild-policy": _policy(
                {
                    "Effect": "Allow",
                    "Action": ["ecr:GetAuthorizationToken"],
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "ecr:BatchCheckLayerAvailability",
                        "ecr:GetDownloadUrlForLayer",
                        "ecr:BatchGetImage",
                        "ecr:PutImage",
                        "ecr:InitiateLayerUpload",
                        "ecr:UploadLayerPart",
                        "ecr:CompleteLayerUpload",
                    ],
                    "Resource": f"arn:aws:ecr:{region}:{account_id}:repository/*",
                },
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                    "Resource": f"arn:aws:s3:::{state_bucket}/codebuild/*",
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "logs:CreateLogGroup",
                        "logs:CreateLogStream",
                        "logs:PutLogEvents",
                    ],
                    "Resource": (
                        f"arn:aws:logs:{region}:{account_id}:log-group:"
                        f"/aws/codebuild/{project_name}*"
                    ),
                },
            )
        },
        permissions_boundary=permissions_boundary,
    )


def backup_role(name: str, *, permissions_boundary: str | None) -> RoleSpec:
    """AWS Backup のサービスロール (stage 単位)。

    AWSBackupDefaultServiceRole は console 初回操作で作られるもので、API しか
    使わないアカウントには存在しないため pocket が作る。
    """
    return RoleSpec(
        name=name,
        service="backup.amazonaws.com",
        managed_policy_arns=BACKUP_ROLE_POLICIES,
        permissions_boundary=permissions_boundary,
    )


def ensure_role(iam_client: IAMClient, spec: RoleSpec) -> str:
    """role を冪等に ensure して ARN を返す。

    既存 role はそのまま返す (policy の差分更新はしない)。新規作成時は
    伝播待ちをしてから返す。
    """
    try:
        return iam_client.get_role(RoleName=spec.name)["Role"]["Arn"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
    echo.log("Creating IAM role: %s" % spec.name)
    create_kwargs: dict[str, Any] = {
        "RoleName": spec.name,
        "AssumeRolePolicyDocument": json.dumps(spec.assume_role_policy),
    }
    if spec.permissions_boundary:
        create_kwargs["PermissionsBoundary"] = spec.permissions_boundary
    role_arn: str = iam_client.create_role(**create_kwargs)["Role"]["Arn"]
    for policy_arn in spec.managed_policy_arns:
        iam_client.attach_role_policy(RoleName=spec.name, PolicyArn=policy_arn)
    for policy_name, doc in spec.inline_policies.items():
        iam_client.put_role_policy(
            RoleName=spec.name,
            PolicyName=policy_name,
            PolicyDocument=json.dumps(doc),
        )
    echo.log("Waiting for IAM role propagation...")
    time.sleep(ROLE_PROPAGATION_WAIT_SECONDS)
    return role_arn


def delete_role(
    iam_client: IAMClient, role_name: str, managed_policy_arns: list[str] | None = None
) -> bool:
    """role を付属の policy ごと削除する (無ければ False)。

    managed policy は呼び出し側が渡したもの (= spec のもの) だけを外す。
    ListAttachedRolePolicies は deploy 権限 (pocket/permissions.py) に無いため
    使わない。inline policy は一覧を取って全部消す。
    """
    try:
        for policy_arn in managed_policy_arns or []:
            try:
                iam_client.detach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
            except ClientError as e:
                if e.response["Error"]["Code"] != "NoSuchEntity":
                    raise
        inline = iam_client.list_role_policies(RoleName=role_name)
        for policy_name in inline["PolicyNames"]:
            iam_client.delete_role_policy(RoleName=role_name, PolicyName=policy_name)
        iam_client.delete_role(RoleName=role_name)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchEntity":
            return False
        raise
    echo.log("Deleted IAM role: %s" % role_name)
    return True
