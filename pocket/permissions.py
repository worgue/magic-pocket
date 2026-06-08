"""pocket.toml の構成から必要な AWS IAM Action 一覧を算出する。

`docs/permissions/aws.md` のテーブルをコード化したもの。
worgue 側の GitHub Actions デプロイ用 IAM Role 等が、`*:*` を避けて
必要最小限の Action を inline policy に組み込めるようにするためのデータソース。
"""

from __future__ import annotations

from .settings import Settings

# コア権限（pocket deploy には常に必要）
_CORE_ACTIONS: list[str] = [
    "cloudformation:*",
    "ecr:*",
    "lambda:*",
    "apigateway:*",
    "s3:*",
    "iam:CreateRole",
    "iam:DeleteRole",
    "iam:GetRole",
    "iam:PutRolePolicy",
    "iam:DeleteRolePolicy",
    "iam:AttachRolePolicy",
    "iam:DetachRolePolicy",
    "iam:PassRole",
    "iam:TagRole",
    "iam:UntagRole",
    "iam:ListRoleTags",
    "logs:*",
    "sts:GetCallerIdentity",
]

# secrets.store == "sm" (デフォルト) または awscontainer.secrets 未設定時
_SM_ACTIONS: list[str] = ["secretsmanager:*"]

# secrets.store == "ssm" 時
_SSM_ACTIONS: list[str] = [
    "ssm:GetParameter",
    "ssm:PutParameter",
    "ssm:DeleteParameters",
    "ssm:GetParametersByPath",
]

# [cloudfront.*] が一つ以上ある時
_CLOUDFRONT_ACTIONS: list[str] = [
    "cloudfront:*",
    "acm:RequestCertificate",
    "acm:DescribeCertificate",
    "acm:DeleteCertificate",
    "route53:ListHostedZones",
    "route53:ChangeResourceRecordSets",
    "route53:GetChange",
]

# [cloudfront.*.waf] が一つ以上ある時
# Deploy 時の CFn 経由作成 + `pocket waf ip ...` CLI の update_ip_set を許可
_WAF_ACTIONS: list[str] = ["wafv2:*"]

# awscontainer.vpc が設定されている時
_VPC_ACTIONS: list[str] = ["ec2:*"]

# [rds] が設定されている時
_RDS_ACTIONS: list[str] = ["rds:*", "ec2:*SecurityGroup*"]

# awscontainer.vpc.efs が設定されている時
_EFS_ACTIONS: list[str] = ["elasticfilesystem:*"]

# いずれかのハンドラに sqs 設定がある時
_SQS_ACTIONS: list[str] = ["sqs:*"]

# [ses] が設定されている時
_SES_ACTIONS: list[str] = ["ses:SendEmail", "ses:SendRawEmail"]

# awscontainer.build.backend == "codebuild" (デフォルト) 時
_CODEBUILD_ACTIONS: list[str] = ["codebuild:*"]


def _uses_ssm(settings: Settings) -> bool:
    ac = settings.awscontainer
    return bool(ac and ac.secrets and ac.secrets.store == "ssm")


def _has_sqs_handler(settings: Settings) -> bool:
    ac = settings.awscontainer
    return bool(ac and any(h.sqs is not None for h in ac.handlers.values()))


def _has_vpc(settings: Settings) -> bool:
    return bool(settings.awscontainer and settings.awscontainer.vpc)


def _has_waf(settings: Settings) -> bool:
    return any(cf.waf is not None for cf in settings.cloudfront.values())


def _has_efs(settings: Settings) -> bool:
    ac = settings.awscontainer
    return bool(ac and ac.vpc and ac.vpc.efs)


def _uses_codebuild(settings: Settings) -> bool:
    ac = settings.awscontainer
    return bool(ac and ac.build.backend == "codebuild")


def compute_actions(settings: Settings) -> list[str]:
    """settings から必要な AWS Action 一覧を算出する。

    順序は `docs/permissions/aws.md` のテーブル順を踏襲し、出力は決定的。
    重複は最初の出現位置を保ったまま除去する。
    """
    rules: list[tuple[bool, list[str]]] = [
        (True, _CORE_ACTIONS),
        (_uses_ssm(settings), _SSM_ACTIONS),
        (not _uses_ssm(settings), _SM_ACTIONS),
        (bool(settings.cloudfront), _CLOUDFRONT_ACTIONS),
        (_has_waf(settings), _WAF_ACTIONS),
        (_has_vpc(settings), _VPC_ACTIONS),
        (settings.rds is not None, _RDS_ACTIONS),
        (_has_efs(settings), _EFS_ACTIONS),
        (_has_sqs_handler(settings), _SQS_ACTIONS),
        (settings.ses is not None, _SES_ACTIONS),
        (_uses_codebuild(settings), _CODEBUILD_ACTIONS),
    ]

    seen: set[str] = set()
    deduped: list[str] = []
    for condition, group in rules:
        if not condition:
            continue
        for action in group:
            if action not in seen:
                seen.add(action)
                deduped.append(action)
    return deduped
