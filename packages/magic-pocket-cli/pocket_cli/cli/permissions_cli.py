"""`pocket permissions` サブコマンド群。

- `list`: `pocket.toml` の構成から deploy に必要な AWS IAM Action を出力する。
  外部の IAM Role プロビジョニング処理が GitHub Actions デプロイ用 IAM Role を
  作る際の inline policy 生成に使用することを想定。
- `roles`: pocket の Lambda / scheduler / CodeBuild / AWS Backup が使う IAM role
  の定義を出力する。`[iam] external_roles = true` で role を事前に作る用途。
"""

from __future__ import annotations

import json

import boto3
import click

from pocket.context import Context
from pocket.permissions import compute_actions
from pocket.settings import Settings
from pocket_cli.resources.aws.stage_roles import stage_role_specs


@click.group()
def permissions():
    """IAM 権限関連のサブコマンド。"""


@permissions.command("list")
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option(
    "--format",
    "format_",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help='出力形式。text: 1 行 1 Action / json: {"actions": [...]}',
)
def list_(stage: str, format_: str):
    """pocket.toml から必要な AWS Action 一覧を出力する。

    docs/permissions/aws.md のテーブルに基づき、`[cloudfront]` / `[rds]` /
    `[ses]` などの設定有無に応じて必要 Action を組み立てる。粒度は
    ワイルドカード中心 (`cloudformation:*` 等)。
    """
    settings = Settings.from_toml(stage=stage)
    actions = compute_actions(settings)
    if format_ == "json":
        click.echo(json.dumps({"actions": actions}, indent=2))
    else:
        for action in actions:
            click.echo(action)


@permissions.command("roles")
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option(
    "--account-id",
    help="role の ARN と policy に埋め込む AWS account ID。省略時は"
    " sts:GetCallerIdentity で取得する。",
)
def roles(stage: str, account_id: str | None):
    """pocket が使う IAM role の定義を JSON で出力する。

    role ごとに名前・ARN・信頼ポリシー・managed policy・inline policy を出す。
    policy 中の CloudFormation 変数 (${AWS::Region} 等) は展開済みなので、
    そのまま IAM の CreateRole / PutRolePolicy に渡せる。`[iam] external_roles
    = true` の stage では、deploy がこの定義と実際の role を突き合わせる。
    """
    context = Context.from_toml(stage=stage)
    if not context.general:
        raise click.ClickException("[general] が見つかりません")
    region = context.general.region
    account = (
        account_id
        or (boto3.client("sts", region_name=region).get_caller_identity()["Account"])
    )
    specs = stage_role_specs(context, account_id=account)
    output = {
        "stage": stage,
        "region": region,
        "account_id": account,
        "external_roles": context.external_roles,
        "roles": [
            {
                "name": spec.name,
                "arn": f"arn:aws:iam::{account}:role/{spec.name}",
                "assume_role_policy": spec.assume_role_policy,
                "managed_policy_arns": spec.managed_policy_arns,
                "inline_policies": spec.resolved_inline_policies(
                    region=region, account_id=account
                ),
            }
            for spec in specs
        ],
    }
    click.echo(json.dumps(output, indent=2, ensure_ascii=False))
