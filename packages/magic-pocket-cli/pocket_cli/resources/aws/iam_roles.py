"""pocket が作る IAM role の定義と、その作成・削除。

pocket が作る role はここに列挙した 4 種だけで、どれも RoleSpec (名前・信頼する
service・managed policy・inline policy・boundary) で表す。作成経路は 2 通り:

- CFn: Lambda 実行 role / scheduler role。container stack のテンプレートが
  RoleSpec.cfn_properties をそのまま埋め込む
- API: CodeBuild role / AWS Backup role。stack を持たないリソースから
  ensure_role / delete_role で作る

inline policy の Resource には CFn の Fn::Sub が入りうる (CFn 経路の role のみ)。
Fn::GetAtt は使わない (role を stack の外で事前に作れるよう、値はどれも
pocket.toml と account / region だけで決まる)。

[iam] external_roles = true のときは pocket は role を作らない。RoleSpec は
「利用者が事前に作るべき role」の定義になり、`pocket permissions roles` が
それを出力する。deploy は verify_role で role の存在と信頼先だけを確認し、
policy の中身は見ない (充足は role の持ち主の責任。pocket の版更新で policy の
文面が変わるたびに role の更新を強いない)。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError

from pocket.utils import echo

if TYPE_CHECKING:
    from mypy_boto3_iam import IAMClient

    from pocket.context import (
        ContainerContext,
        DsqlContext,
        RdsContext,
        SchedulerContext,
    )
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
    # 信頼ポリシーに Version を書くか。CFn は AssumeRolePolicyDocument が変わると
    # iam:UpdateAssumeRolePolicy を呼ぶが、deploy 権限には含めていない (boundary
    # 条件を付けられず、既存 role の信頼先を書き換えられる権限のため)。不足すると
    # rollback も同じ権限で失敗し UPDATE_ROLLBACK_FAILED になる。
    # 既存 stack の文書と一致させる必要がある role だけ False にする
    assume_role_policy_version: bool = True
    # [iam] external_roles。pocket は作らず、利用者が事前に作った role を使う
    external: bool = False

    def cfn_arn(self, logical_id: str) -> dict[str, Any]:
        """テンプレートからこの role の ARN を参照する式。"""
        if self.external:
            return _sub(
                f"arn:${{AWS::Partition}}:iam::${{AWS::AccountId}}:role/{self.name}"
            )
        return {"Fn::GetAtt": f"{logical_id}.Arn"}

    def resolved_inline_policies(
        self, *, region: str, account_id: str
    ) -> dict[str, dict[str, Any]]:
        """Fn::Sub を展開した inline policy (IAM に直接書ける形)。"""
        return {
            name: resolve_intrinsics(doc, region=region, account_id=account_id)
            for name, doc in self.inline_policies.items()
        }

    @property
    def assume_role_policy(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": self.service},
                    "Action": "sts:AssumeRole",
                }
            ]
        }
        if self.assume_role_policy_version:
            document = {"Version": _POLICY_VERSION, **document}
        return document

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
    rds: RdsContext | None = None,
    dsql: DsqlContext | None = None,
) -> RoleSpec:
    """container の Lambda 実行 role (全 handler 共通)。

    DB の権限は pocket.toml から決まる値 (識別子・タグ) だけで組み立て、deploy 時に
    AWS から解決した ARN には依存しない。role を DB より先に作れるようにするため
    (RDS の managed secret や DSQL cluster の ARN は作成時に AWS が乱数で決める)。
    """
    prefix = ctx.resource_prefix
    policies: dict[str, dict[str, Any]] = {}
    for inlet in ctx.inbound.values():
        policies[f"inbound-{inlet.name}"] = _inbound_policy(inlet)
    policies.update(_secrets_policies(ctx))
    if rds:
        policies.update(_rds_policies(prefix, rds))
    if dsql:
        policies[f"{prefix}access-dsql"] = _dsql_policy(dsql)
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

    if ctx.external_roles:
        # CFn が持つ既存 role (下の名前) と衝突しないよう別名にする。切替時は
        # CFn が旧 role を消す間も、事前に作った新 role で Lambda が動き続ける
        return RoleSpec(
            name=f"{prefix}{ctx.name}-lambda-role",
            service="lambda.amazonaws.com",
            managed_policy_arns=_lambda_managed_policies(ctx),
            inline_policies=policies,
            external=True,
        )
    return RoleSpec(
        name=f"lambda-{ctx.slug}-{ctx.name}-{ctx.namespace}",
        service="lambda.amazonaws.com",
        managed_policy_arns=_lambda_managed_policies(ctx),
        inline_policies=policies,
        permissions_boundary=ctx.permissions_boundary,
        # 0.38.0 以前の container stack は Version なしで LambdaRole を作っている
        assume_role_policy_version=False,
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


def _rds_policies(prefix: str, rds: RdsContext) -> dict[str, dict[str, Any]]:
    """RDS の認証情報を読む権限。

    secret はどれも既定の aws/secretsmanager key で暗号化されている (pocket は
    KMS key を指定しない) ため、kms:Decrypt は要らない。
    """
    if not rds.managed:
        if not rds.secret_arn:
            return {}
        return {
            f"{prefix}access-rds-secret": _policy(
                _allow(["secretsmanager:GetSecretValue"], [rds.secret_arn])
            )
        }
    strategy, store = rds.password_strategy, rds.secret_store
    if strategy == "static":
        name = rds.credentials_secret_name
        if store == "ssm":
            return {
                f"{prefix}access-rds-ssm": _policy(
                    _allow(
                        ["ssm:GetParameter"],
                        [
                            _sub(
                                "arn:aws:ssm:${AWS::Region}:${AWS::AccountId}"
                                f":parameter/{name}"
                            )
                        ],
                    )
                )
            }
        # Secrets Manager は secret 名の後ろに "-" + 6 文字の乱数を付けて ARN にする
        resource = _sub(
            f"arn:${{AWS::Partition}}:secretsmanager:{rds.region}:${{AWS::AccountId}}"
            f":secret:{name}-??????"
        )
        return {
            f"{prefix}access-rds-secret": _policy(
                _allow(["secretsmanager:GetSecretValue"], [resource])
            )
        }
    # aws-managed: RDS が作る secret (rds!cluster-<uuid>) には元の cluster の ARN が
    # aws: タグで付く。aws: タグは利用者が付けられないので、別の secret が名乗る
    # ことはない
    resource = _sub(
        f"arn:${{AWS::Partition}}:secretsmanager:{rds.region}:${{AWS::AccountId}}"
        ":secret:rds!cluster-*"
    )
    cluster_arn = _sub(
        f"arn:${{AWS::Partition}}:rds:{rds.region}:${{AWS::AccountId}}"
        f":cluster:{rds.cluster_identifier}"
    )
    return {
        f"{prefix}access-rds-secret": _policy(
            {
                **_allow(["secretsmanager:GetSecretValue"], [resource]),
                "Condition": {
                    "StringEquals": {
                        "aws:ResourceTag/aws:rds:primaryDBClusterArn": cluster_arn
                    }
                },
            }
        )
    }


def _dsql_policy(dsql: DsqlContext) -> dict[str, Any]:
    """DSQL に admin で接続する権限。cluster は pocket が付ける Name タグで絞る。"""
    resource = _sub(
        f"arn:${{AWS::Partition}}:dsql:{dsql.region}:${{AWS::AccountId}}:cluster/*"
    )
    return _policy(
        {
            **_allow(["dsql:DbConnectAdmin"], [resource]),
            "Condition": {"StringEquals": {"aws:ResourceTag/Name": dsql.tag_name}},
        }
    )


def scheduler_role(
    scheduler: SchedulerContext,
    *,
    permissions_boundary: str | None,
    external: bool = False,
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
    if scheduler.sqs_queue_arns:
        statements.append(
            _allow(["sqs:SendMessage"], [_sub(arn) for arn in scheduler.sqs_queue_arns])
        )
    return RoleSpec(
        name=scheduler.role_name,
        service="scheduler.amazonaws.com",
        inline_policies={"invoke-handlers": _policy(*statements)},
        # boundary 強制 account (iam:CreateRole が boundary 付きのみ許可) では
        # Lambda role と同様に boundary が無いと作成できない
        permissions_boundary=permissions_boundary,
        external=external,
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
    external: bool = False,
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
        external=external,
    )


def backup_role(
    name: str, *, permissions_boundary: str | None, external: bool = False
) -> RoleSpec:
    """AWS Backup のサービスロール (stage 単位)。

    AWSBackupDefaultServiceRole は console 初回操作で作られるもので、API しか
    使わないアカウントには存在しないため pocket が作る。
    """
    return RoleSpec(
        name=name,
        service="backup.amazonaws.com",
        managed_policy_arns=BACKUP_ROLE_POLICIES,
        permissions_boundary=permissions_boundary,
        external=external,
    )


def ensure_role(iam_client: IAMClient, spec: RoleSpec) -> str:
    """role を冪等に ensure して ARN を返す。

    既存 role はそのまま返す (policy の差分更新はしない)。新規作成時は
    伝播待ちをしてから返す。external の role は作らず、検査して ARN を返す。
    """
    if spec.external:
        return verify_role(iam_client, spec)
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


# --- external_roles ---


class RoleMismatchError(Exception):
    """external_roles の role が無い、または pocket の service を信頼していない。"""


_SUB_VARIABLE = re.compile(r"\$\{([^}]+)\}")


def resolve_intrinsics(value: Any, *, region: str, account_id: str) -> Any:
    """policy 文書中の Fn::Sub を展開する。他の組み込み関数は扱わない。"""
    if isinstance(value, dict):
        if set(value) == {"Fn::Sub"}:
            return _SUB_VARIABLE.sub(
                lambda m: _pseudo_parameter(m.group(1), region, account_id),
                value["Fn::Sub"],
            )
        return {
            k: resolve_intrinsics(v, region=region, account_id=account_id)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            resolve_intrinsics(v, region=region, account_id=account_id) for v in value
        ]
    return value


def _pseudo_parameter(name: str, region: str, account_id: str) -> str:
    values = {
        "AWS::Region": region,
        "AWS::AccountId": account_id,
        "AWS::Partition": "aws",
    }
    if name not in values:
        raise ValueError("展開できない Fn::Sub の変数です: ${%s}" % name)
    return values[name]


def verify_role(iam_client: IAMClient, spec: RoleSpec) -> str:
    """external の role の存在と pocket の service への信頼を検査し、ARN を返す。

    policy (managed / inline) の中身は検査しない。role の持ち主は組織の流儀
    (標準の許可セット・命名・boundary) で policy を組むため、pocket の出力との
    文面一致を求めると、pocket の版更新で文面が変わるたびに全 project の deploy
    が止まる。必要な権限は `pocket permissions roles` が示し、充足は持ち主が
    保証する。
    """
    role = _get_external_role(iam_client, spec.name)
    if not _trusts_service(role.get("AssumeRolePolicyDocument", {}), spec.service):
        raise RoleMismatchError(
            "IAM role %s の信頼ポリシーが %s に sts:AssumeRole を許可していません。"
            "`pocket permissions roles` の assume_role_policy を参考に信頼ポリシーを"
            "直してから再実行してください。" % (spec.name, spec.service)
        )
    return role["Arn"]


def _get_external_role(iam_client: IAMClient, name: str) -> dict[str, Any]:
    try:
        return dict(iam_client.get_role(RoleName=name)["Role"])
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        raise RoleMismatchError(
            "IAM role %s がありません。[iam] external_roles = true では pocket は"
            " role を作りません。`pocket permissions roles` の出力どおりに作成して"
            "から再実行してください。" % name
        ) from e


def _trusts_service(document: dict[str, Any], service: str) -> bool:
    statements = document.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    for statement in statements:
        if statement.get("Effect") != "Allow":
            continue
        principal = statement.get("Principal", {})
        services = principal.get("Service", []) if isinstance(principal, dict) else []
        if isinstance(services, str):
            services = [services]
        actions = statement.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        if service in services and "sts:AssumeRole" in actions:
            return True
    return False
