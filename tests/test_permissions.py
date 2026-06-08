"""pocket.permissions.compute_actions と pocket permissions list CLI のテスト。"""

from __future__ import annotations

import json

from click.testing import CliRunner
from pocket_cli.cli.permissions_cli import permissions

from pocket.permissions import action_groups, compute_actions
from pocket.settings import Settings

# 長いハンドラ command を test 内で 90 列に収めるための定数化
_SQS_HANDLER_CMD = (
    "pocket.django.lambda_handlers.sqs_management_command_report_failuers_handler"
)


def _build_settings(**overrides) -> Settings:
    """テスト用の最小 Settings を組み立てる。overrides で追加セクションを上書き。"""
    data: dict = {
        "stage": "dev",
        "general": {
            "region": "ap-northeast-1",
            "project_name": "testprj",
            "stages": ["dev", "prod"],
        },
    }
    data.update(overrides)
    return Settings.model_validate(data)


def test_minimal_no_awscontainer():
    """awscontainer が無い極小構成 — core + secretsmanager のみ。"""
    settings = _build_settings()
    actions = compute_actions(settings)
    assert "cloudformation:*" in actions
    assert "secretsmanager:*" in actions
    # CFn が LambdaRole に Tag を付与するため Tag 系 IAM Action も core に含む
    assert "iam:TagRole" in actions
    assert "iam:UntagRole" in actions
    assert "iam:ListRoleTags" in actions
    # オプション系は一切含まれない
    assert "cloudfront:*" not in actions
    assert "ec2:*" not in actions
    assert "rds:*" not in actions
    assert "elasticfilesystem:*" not in actions
    assert "sqs:*" not in actions
    assert "ses:SendEmail" not in actions
    assert "codebuild:*" not in actions
    assert "ssm:GetParameter" not in actions


def test_awscontainer_default_includes_codebuild():
    """awscontainer のみ (build backend デフォルト) で codebuild:* が付く。"""
    settings = _build_settings(
        awscontainer={
            "dockerfile_path": "Dockerfile",
            "handlers": {
                "wsgi": {"command": "pocket.django.lambda_handlers.wsgi_handler"}
            },
        }
    )
    actions = compute_actions(settings)
    assert "codebuild:*" in actions
    assert "secretsmanager:*" in actions
    assert "sqs:*" not in actions
    assert "ec2:*" not in actions


def test_secrets_store_ssm():
    """secrets.store == "ssm" のとき ssm 系 Action のみ。"""
    settings = _build_settings(
        awscontainer={
            "dockerfile_path": "Dockerfile",
            "secrets": {"store": "ssm"},
            "handlers": {},
        }
    )
    actions = compute_actions(settings)
    assert "secretsmanager:*" not in actions
    assert "ssm:GetParameter" in actions
    assert "ssm:PutParameter" in actions
    assert "ssm:DeleteParameters" in actions
    assert "ssm:GetParametersByPath" in actions


def test_cloudfront_adds_acm_route53():
    """[cloudfront.*] があれば cloudfront / acm / route53 Action が入る。"""
    settings = _build_settings(
        s3={},
        awscontainer={"dockerfile_path": "Dockerfile", "handlers": {}},
        cloudfront={
            "main": {
                "routes": [
                    {"is_default": True, "is_spa": True, "origin_path": "/main"},
                ],
            },
        },
    )
    actions = compute_actions(settings)
    assert "cloudfront:*" in actions
    # KVS への token_secret 書込みは別 service prefix のため明示的に必要
    assert "cloudfront-keyvaluestore:*" in actions
    assert "acm:RequestCertificate" in actions
    assert "acm:DescribeCertificate" in actions
    assert "acm:DeleteCertificate" in actions
    assert "route53:ListHostedZones" in actions
    assert "route53:ChangeResourceRecordSets" in actions
    assert "route53:GetChange" in actions
    # waf block 未設定なら wafv2:* は入らない
    assert "wafv2:*" not in actions


def test_cloudfront_waf_adds_wafv2():
    """[cloudfront.*.waf] block があれば wafv2:* も追加される。"""
    settings = _build_settings(
        s3={},
        awscontainer={"dockerfile_path": "Dockerfile", "handlers": {}},
        cloudfront={
            "main": {
                "routes": [
                    {"is_default": True, "is_spa": True, "origin_path": "/main"},
                ],
                "waf": {},
            },
        },
    )
    actions = compute_actions(settings)
    assert "wafv2:*" in actions


def test_vpc_adds_ec2():
    settings = _build_settings(
        awscontainer={
            "dockerfile_path": "Dockerfile",
            "vpc": {"ref": "main", "zone_suffixes": ["a", "c"]},
            "handlers": {},
        },
    )
    actions = compute_actions(settings)
    assert "ec2:*" in actions
    assert "elasticfilesystem:*" not in actions


def test_vpc_with_efs_adds_efs():
    settings = _build_settings(
        awscontainer={
            "dockerfile_path": "Dockerfile",
            "vpc": {"ref": "main", "zone_suffixes": ["a", "c"], "efs": {}},
            "handlers": {},
        },
    )
    actions = compute_actions(settings)
    assert "ec2:*" in actions
    assert "elasticfilesystem:*" in actions


def test_rds_adds_rds_and_sg():
    """rds のみ (VPC 経由) — RDS Action 群が入る。"""
    settings = _build_settings(
        vpc={"ref": "main", "zone_suffixes": ["a", "c"]},
        awscontainer={
            "dockerfile_path": "Dockerfile",
            "handlers": {},
        },
        rds={},
    )
    actions = compute_actions(settings)
    assert "rds:*" in actions
    assert "ec2:*SecurityGroup*" in actions


def test_sqs_handler_adds_sqs():
    settings = _build_settings(
        awscontainer={
            "dockerfile_path": "Dockerfile",
            "handlers": {
                "worker": {"command": _SQS_HANDLER_CMD, "sqs": {}},
            },
        },
    )
    actions = compute_actions(settings)
    assert "sqs:*" in actions


def test_ses_adds_ses_actions():
    settings = _build_settings(
        awscontainer={"dockerfile_path": "Dockerfile", "handlers": {}},
        ses={"from_email": "noreply@example.com"},
    )
    actions = compute_actions(settings)
    assert "ses:SendEmail" in actions
    assert "ses:SendRawEmail" in actions


def test_build_backend_docker_drops_codebuild():
    settings = _build_settings(
        awscontainer={
            "dockerfile_path": "Dockerfile",
            "handlers": {},
            "build": {"backend": "docker"},
        },
    )
    actions = compute_actions(settings)
    assert "codebuild:*" not in actions


def test_full_config_contains_everything():
    """フル構成 — docs/permissions/aws.md のすべての項目が含まれる。"""
    settings = _build_settings(
        vpc={"ref": "main", "zone_suffixes": ["a", "c"]},
        s3={},
        awscontainer={
            "dockerfile_path": "Dockerfile",
            "vpc": {"ref": "main", "zone_suffixes": ["a", "c"], "efs": {}},
            "handlers": {
                "wsgi": {"command": "pocket.django.lambda_handlers.wsgi_handler"},
                "worker": {"command": _SQS_HANDLER_CMD, "sqs": {}},
            },
        },
        rds={},
        ses={"from_email": "noreply@example.com"},
        cloudfront={
            "main": {
                "routes": [
                    {"is_default": True, "is_spa": True, "origin_path": "/main"},
                ],
            },
        },
    )
    actions = compute_actions(settings)
    expected = {
        "cloudformation:*",
        "ecr:*",
        "lambda:*",
        "apigateway:*",
        "s3:*",
        "iam:PassRole",
        "logs:*",
        "sts:GetCallerIdentity",
        "secretsmanager:*",
        "cloudfront:*",
        "acm:RequestCertificate",
        "route53:GetChange",
        "ec2:*",
        "rds:*",
        "ec2:*SecurityGroup*",
        "elasticfilesystem:*",
        "sqs:*",
        "ses:SendEmail",
        "codebuild:*",
    }
    assert expected.issubset(set(actions))


def test_no_duplicates():
    """RDS と VPC の組み合わせでも `ec2:*SecurityGroup*` 等が重複しない。"""
    settings = _build_settings(
        vpc={"ref": "main", "zone_suffixes": ["a", "c"]},
        awscontainer={
            "dockerfile_path": "Dockerfile",
            "vpc": {"ref": "main", "zone_suffixes": ["a", "c"]},
            "handlers": {},
        },
        rds={},
    )
    actions = compute_actions(settings)
    assert len(actions) == len(set(actions))


def test_action_groups_public_keys_stable():
    """public API: action_groups() のキー集合が安定した group 名であること。"""
    groups = action_groups()
    assert set(groups.keys()) == {
        "core",
        "ssm",
        "secretsmanager",
        "cloudfront",
        "waf",
        "vpc",
        "rds",
        "efs",
        "sqs",
        "ses",
        "codebuild",
    }
    # core は常時付与群。代表的な Action を含む
    assert "cloudformation:*" in groups["core"]
    assert "iam:TagRole" in groups["core"]
    # 二層ずれの再発防止対象だった Action も group 経由で参照できる
    assert "route53:ListHostedZones" in groups["cloudfront"]
    assert "cloudfront-keyvaluestore:*" in groups["cloudfront"]


def test_action_groups_returns_copies():
    """呼び出し側が返り値を変更しても内部状態・後続呼び出しに影響しないこと。"""
    groups = action_groups()
    groups["core"].append("mutated:*")
    assert "mutated:*" not in action_groups()["core"]


def test_action_groups_is_single_source_for_compute_actions():
    """compute_actions の出力が action_groups() の group 内容に被覆されること。

    外部ツール側 guard test (BASELINE_ACTIONS が常時付与群を被覆しているか) が
    依存する不変条件: compute_actions が返す Action はすべて
    action_groups() のいずれかの group に属する。
    """
    groups = action_groups()
    union = {action for actions in groups.values() for action in actions}
    settings = _build_settings(
        vpc={"ref": "main", "zone_suffixes": ["a", "c"]},
        s3={},
        awscontainer={
            "dockerfile_path": "Dockerfile",
            "vpc": {"ref": "main", "zone_suffixes": ["a", "c"], "efs": {}},
            "handlers": {
                "wsgi": {"command": "pocket.django.lambda_handlers.wsgi_handler"},
                "worker": {"command": _SQS_HANDLER_CMD, "sqs": {}},
            },
        },
        rds={},
        ses={"from_email": "noreply@example.com"},
        cloudfront={
            "main": {
                "routes": [
                    {"is_default": True, "is_spa": True, "origin_path": "/main"},
                ],
            },
        },
    )
    assert set(compute_actions(settings)).issubset(union)


def test_cli_text_output(use_toml):
    use_toml("tests/data/toml/default.toml")
    runner = CliRunner()
    result = runner.invoke(permissions, ["list", "--stage", "dev"])
    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert "cloudformation:*" in lines
    assert "cloudfront:*" in lines  # default.toml には [cloudfront.main] がある
    assert "ec2:*" in lines  # [vpc] と awscontainer.vpc がある


def test_cli_json_output(use_toml):
    use_toml("tests/data/toml/default.toml")
    runner = CliRunner()
    result = runner.invoke(permissions, ["list", "--stage", "dev", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "actions" in payload
    assert isinstance(payload["actions"], list)
    assert "cloudformation:*" in payload["actions"]
    # JSON 出力でも重複なし
    assert len(payload["actions"]) == len(set(payload["actions"]))
