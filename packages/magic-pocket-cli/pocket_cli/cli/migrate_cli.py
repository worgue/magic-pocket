"""pocket migrate: スタックのテンプレートハッシュタグを一括付与する。

旧バージョンでデプロイされたスタックに pocket:template_hash タグを付与し、
yaml_synced の判定をハッシュベースに移行する。
"""

from __future__ import annotations

import hashlib

import click
import yaml

from pocket.context import Context
from pocket.utils import echo
from pocket_cli.cli.deploy_cli import get_resources


def _get_existing_stacks(context: Context) -> list:
    """デプロイ済みの全スタックを収集する"""
    stacks = []
    for resource in get_resources(context):
        if hasattr(resource, "stack") and resource.stack.exists:
            stacks.append(resource.stack)
    return stacks


def _compute_uploaded_hash(stack) -> str | None:
    """uploaded template からハッシュを計算する"""
    uploaded = stack.uploaded_template
    if uploaded is None:
        return None
    if isinstance(uploaded, str):
        return hashlib.sha256(uploaded.encode()).hexdigest()[:16]
    return hashlib.sha256(
        yaml.dump(dict(uploaded), sort_keys=True).encode()
    ).hexdigest()[:16]


def _backfill_tags(stacks: list) -> None:
    """タグがないスタックに uploaded template のハッシュでタグを付与する"""
    for stack in stacks:
        if stack._deployed_template_hash is not None:
            continue
        h = _compute_uploaded_hash(stack)
        if h is None:
            continue
        echo.log("タグ付与 (uploaded hash): %s (%s)" % (stack.name, h))
        stack.client.update_stack(
            StackName=stack.name,
            UsePreviousTemplate=True,
            Capabilities=stack.capabilities,
            Tags=stack.stack_tags + [{"Key": "pocket:template_hash", "Value": h}],
        )
        stack.wait_status("COMPLETED", timeout=300, interval=5)
        stack.clear_status()
        echo.success("完了: %s" % stack.name)


def _find_stacks_needing_update(stacks: list) -> list:
    """ローカルテンプレートとハッシュが異なるスタックを返す"""
    targets = []
    for stack in stacks:
        if stack._deployed_template_hash == stack._template_hash:
            echo.info("%s: タグは最新です" % stack.name)
            continue
        targets.append(stack)
    return targets


def _apply_local_tags(targets: list) -> None:
    """ローカルテンプレートのハッシュでタグを更新する"""
    for stack in targets:
        echo.log("タグ更新 (local hash): %s" % stack.name)
        stack.client.update_stack(
            StackName=stack.name,
            UsePreviousTemplate=True,
            Capabilities=stack.capabilities,
            Tags=stack._build_tags(),
        )
        stack.wait_status("COMPLETED", timeout=300, interval=5)
        echo.success("完了: %s" % stack.name)


def _check_real_diffs(stacks: list) -> list[str]:
    """非 ASCII 文字化け以外のテンプレート差分があるスタックを返す"""
    needs_deploy = []
    for stack in stacks:
        diff = stack.yaml_diff
        if diff == {}:
            continue
        if set(diff.keys()) == {"values_changed"}:
            all_garbled = all(
                isinstance(c.get("old_value"), str) and "??" in c["old_value"]
                for c in diff["values_changed"].values()
            )
            if all_garbled:
                continue
        needs_deploy.append(stack.name)
    return needs_deploy


@click.command()
@click.option("--stage", envvar="POCKET_STAGE", prompt=True)
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="確認プロンプトをスキップ"
)
def migrate(stage: str, yes: bool):
    """スタックのテンプレートハッシュタグを一括付与する"""
    from pocket_cli.cli.aws_auth import check_aws_credentials

    check_aws_credentials()
    context = Context.from_toml(stage=stage)
    stacks = _get_existing_stacks(context)

    if not stacks:
        echo.warning("対象のスタックがありません。")
        return

    # 1. タグがないスタックに uploaded template のハッシュで仮タグを付与
    _backfill_tags(stacks)

    # 2. ローカルとの差分チェック（非 ASCII 文字化け以外の差分があれば中断）
    needs_deploy = _check_real_diffs(stacks)
    if needs_deploy:
        echo.danger("以下のスタックにテンプレートの差分があります:")
        for name in needs_deploy:
            echo.info("  - %s" % name)
        echo.danger(
            "先に pocket deploy --stage=%s で最新バージョンを"
            "デプロイしてから再実行してください。" % stage
        )
        raise SystemExit(1)

    # 3. ローカルテンプレートのハッシュでタグを更新
    targets = _find_stacks_needing_update(stacks)
    if not targets:
        echo.success("全スタックのタグが最新です。")
        return

    echo.info("以下のスタックのタグをローカルハッシュに更新します:")
    for stack in targets:
        echo.info("  - %s" % stack.name)
    if not yes:
        click.confirm("実行しますか？", abort=True)

    _apply_local_tags(targets)
    echo.success("全スタックのマイグレーションが完了しました。")
