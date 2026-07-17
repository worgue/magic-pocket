"""pocket migrate: バージョン間のデータ/スタック移行をまとめて実行する。

サブコマンド:

- ``template-hash``: 旧バージョンでデプロイされたスタックに pocket:template_hash
  タグを付与し、yaml_synced の判定をハッシュベースに移行する。
- ``secret-paths``: stored user secret を旧キー基準パス ({pocket_key}-user/{key})
  から新 type 基準パス ({pocket_key}-user/{type}) へ移設する (0.11→0.12)。

サブコマンド無指定 (``pocket migrate``) なら全 migration を冪等に順次実行する。
"""

from __future__ import annotations

import hashlib

import click
import yaml

from pocket import __version__, secret_store
from pocket.context import Context, SecretsContext, user_secret_path
from pocket.utils import echo
from pocket_cli.cli.deploy_cli import get_resources

# --- template-hash migration -------------------------------------------------


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


def _run_template_hash(stage: str, *, yes: bool) -> None:
    """スタックのテンプレートハッシュタグを一括付与する (冪等)。

    未 deploy のテンプレ差分がある場合は SystemExit(1) で中断する
    (前提条件ガード: 先に deploy が必要)。
    """
    context = Context.from_toml(stage=stage)
    stacks = _get_existing_stacks(context)

    if not stacks:
        echo.warning("template-hash: 対象のスタックがありません。")
        return

    # 1. タグがないスタックに uploaded template のハッシュで仮タグを付与
    #    (update_stack を伴う CFn mutation のため、確認してから実行する)
    backfill_targets = [s for s in stacks if s._deployed_template_hash is None]
    if backfill_targets and not yes:
        echo.info(
            "以下のスタックにタグ付与のための update_stack "
            "(UsePreviousTemplate) を実行します:"
        )
        for stack in backfill_targets:
            echo.info("  - %s" % stack.name)
        click.confirm("実行しますか？", abort=True)
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
        echo.success("template-hash: 全スタックのタグが最新です。")
        return

    echo.info("以下のスタックのタグをローカルハッシュに更新します:")
    for stack in targets:
        echo.info("  - %s" % stack.name)
    if not yes:
        click.confirm("実行しますか？", abort=True)

    _apply_local_tags(targets)
    echo.success("template-hash: 全スタックの移行が完了しました。")


# --- secret-paths migration (0.11 -> 0.12) -----------------------------------


def _migrate_user_secret_path(
    sc: SecretsContext, key: str, spec, *, dry_run: bool = False
) -> dict:
    """0.11→0.12: user secret を旧キー基準パス→新 type 基準パスへ移設する。

    旧 /{pocket_key}-user/{key} の値を新 /{pocket_key}-user/{type} へ copy し、
    verify 後に旧を delete する。冪等 (新在+旧在なら旧のみ delete して cleanup を
    完了)。戻り値の status: ``migrated`` / ``cleaned`` / ``already`` / ``missing``
    / ``skip-name`` (dry_run では ``would-migrate`` / ``would-clean``)。
    """
    if spec.type is None:  # name モードは移設対象外
        return {"status": "skip-name", "key": key}
    store = spec.store or sc.store
    old_name = user_secret_path(sc.pocket_key, key, store)
    new_name = sc.stored_url_name(spec.type, store)
    info = {
        "status": None,
        "key": key,
        "type": spec.type,
        "old": old_name,
        "new": new_name,
    }
    if old_name == new_name:  # 既に type == key (レア) なら移設不要
        info["status"] = "already"
        return info
    region = sc.region
    new_exists = secret_store.exists_stored_value(new_name, store, region)
    old_exists = secret_store.exists_stored_value(old_name, store, region)
    if new_exists:
        if old_exists:
            if not dry_run:
                secret_store.delete_stored_value(old_name, store, region)
            info["status"] = "would-clean" if dry_run else "cleaned"
        else:
            info["status"] = "already"
        return info
    if not old_exists:
        info["status"] = "missing"
        return info
    if dry_run:
        info["status"] = "would-migrate"
        return info
    value = secret_store.read_stored_value(old_name, store, region)
    if value is None:
        info["status"] = "missing"
        return info
    secret_store.put_stored_value(new_name, store, value, region)
    if secret_store.read_stored_value(new_name, store, region) != value:
        raise RuntimeError("copy verify failed: %s" % new_name)
    secret_store.delete_stored_value(old_name, store, region)
    info["status"] = "migrated"
    return info


_SECRET_PATH_LABELS = {
    "would-migrate": "移設予定 (copy→旧削除)",
    "would-clean": "旧パス削除予定 (移行済)",
    "migrated": "移設完了",
    "cleaned": "旧パス削除 (移行済)",
    "already": "移行不要",
    "missing": "未 provision (store-url で作成してください)",
    "skip-name": "name モード (対象外)",
}


def _report_secret_path(info: dict) -> None:
    status = info["status"]
    label = _SECRET_PATH_LABELS.get(status, status)
    if status in ("migrated", "cleaned"):
        echo.success(
            "%s: %s (%s → %s)" % (info["key"], label, info["old"], info["new"])
        )
    elif status in ("would-migrate", "would-clean"):
        echo.info("%s: %s (%s → %s)" % (info["key"], label, info["old"], info["new"]))
    elif status == "missing":
        echo.warning("%s: %s (%s)" % (info["key"], label, info.get("new")))
    else:
        echo.info("%s: %s" % (info["key"], label))


def _warn_runtime_bump_required(stage: str) -> None:
    """secret-paths 移設で旧パスが削除されることの事前警告。

    移設は copy→旧削除を一括で行うため、この stage に deploy 済みの Lambda runtime
    (magic-pocket[django]) が古いままだと、旧パス (削除済) を参照して DATABASE_URL を
    解決できず cold start の INIT で落ちる。移設後に runtime を CLI と同版へ bump して
    再デプロイするまで stage が黙って壊れる footgun を、移設前に明示する
    (template-hash が「先に deploy」で中断するのと同じ思想の事前ガード)。
    """
    echo.warning(
        "⚠ 移設すると旧パスは削除されます (copy→旧削除は一括)。この stage に "
        "deploy 済みの Lambda runtime (magic-pocket[django]) が古いままだと、"
        "旧パスを参照して DATABASE_URL を解決できず cold start の INIT で落ちます。"
    )
    echo.warning(
        "  移設後は速やかに runtime を CLI と同版へ上げて再デプロイしてください:\n"
        "    uv add 'magic-pocket[django]>=%s'\n"
        "    pocket deploy --stage=%s  (Django なら pocket django deploy --stage=%s)"
        % (__version__, stage, stage)
    )
    echo.warning(
        "  ※ copy と旧削除が一括のため完全な無停止移行にはなりません。warm instance は "
        "recycle まで生存するので、移設直後に再デプロイすれば影響を最小化できます。"
    )


def _run_secret_paths(stage: str, *, yes: bool, dry_run: bool) -> None:
    """stored user secret を旧キー基準→新 type 基準パスへ移設する (冪等)。"""
    context = Context.from_toml(stage=stage)
    sc = context.awscontainer.secrets if context.awscontainer else None
    type_specs = (
        [(k, s) for k, s in sc.user.items() if s.type is not None] if sc else []
    )
    if sc is None or not type_specs:
        echo.info("secret-paths: type-mode の user secret がありません。")
        return

    # まず dry-run で計画を出す (AWS への書込みなし)
    plans = [_migrate_user_secret_path(sc, k, s, dry_run=True) for k, s in type_specs]
    for info in plans:
        _report_secret_path(info)
    to_act = [p for p in plans if p["status"] in ("would-migrate", "would-clean")]
    if not to_act:
        echo.success(
            "secret-paths: 移設対象はありません (全て移行済み or 未 provision)。"
        )
        return
    _warn_runtime_bump_required(stage)
    if dry_run:
        echo.info("secret-paths: --dry-run のため実行はしません。")
        return
    if not yes:
        click.confirm("上記の移設を実行しますか？", abort=True)

    for key, spec in type_specs:
        info = _migrate_user_secret_path(sc, key, spec, dry_run=False)
        _report_secret_path(info)
    echo.success("secret-paths: 移設が完了しました。")


# --- CLI 配線 -----------------------------------------------------------------


@click.group(invoke_without_command=True)
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", default=None)
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="確認プロンプトをスキップ"
)
@click.pass_context
def migrate(ctx: click.Context, stage: str | None, yes: bool):
    """バージョン間の移行をまとめて実行する。

    サブコマンド無指定なら全 migration (secret-paths → template-hash) を
    冪等に順次実行する。
    """
    if ctx.invoked_subcommand is not None:
        return
    # bare 実行: 全 migration を冪等実行
    from pocket_cli.cli.aws_auth import check_aws_credentials

    check_aws_credentials()
    resolved_stage: str = stage or click.prompt("Stage")
    # secret-paths を先に (高速・ガードで中断しない)、template-hash を後に。
    _run_secret_paths(resolved_stage, yes=yes, dry_run=False)
    try:
        _run_template_hash(resolved_stage, yes=yes)
    except SystemExit:
        echo.warning(
            "template-hash はテンプレ差分のため中断しました "
            "(secret-paths は完了済み)。pocket deploy --stage=%s の後に "
            "pocket migrate を再実行してください。" % resolved_stage
        )


@migrate.command(name="template-hash")
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="確認プロンプトをスキップ"
)
def template_hash(stage: str, yes: bool):
    """スタックのテンプレートハッシュタグを一括付与する。"""
    from pocket_cli.cli.aws_auth import check_aws_credentials

    check_aws_credentials()
    _run_template_hash(stage, yes=yes)


@migrate.command(name="secret-paths")
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="確認プロンプトをスキップ"
)
@click.option(
    "--dry-run", is_flag=True, default=False, help="移設内容を表示のみ (書込みなし)"
)
def secret_paths(stage: str, yes: bool, dry_run: bool):
    """stored user secret を旧キー基準→新 type 基準パスへ移設する (0.11→0.12)。"""
    from pocket_cli.cli.aws_auth import check_aws_credentials

    check_aws_credentials()
    _run_secret_paths(stage, yes=yes, dry_run=dry_run)
