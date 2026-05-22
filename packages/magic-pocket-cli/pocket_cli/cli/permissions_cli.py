"""`pocket permissions` サブコマンド群。

`pocket.toml` の構成から必要な AWS IAM Action を算出して出力する。
外部ツール側で GitHub Actions デプロイ用 IAM Role を作る際の inline policy 生成に
使用することを想定。
"""

from __future__ import annotations

import json

import click

from pocket.permissions import compute_actions
from pocket.settings import Settings


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
