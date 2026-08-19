from __future__ import annotations

import webbrowser

import boto3
import click
from botocore.exceptions import ClientError

from pocket.context import Context
from pocket.runtime import get_secrets, resolve_container_name
from pocket.utils import echo
from pocket_cli.cli.destroy_cli import (
    _collect_container_targets,
    _destroy_containers,
)
from pocket_cli.mediator import Mediator
from pocket_cli.resources.container import Container


@click.group()
def container():
    pass


def get_container_resource(stage, container_name: str | None = None):
    """--container 指定 (単一 container なら省略可) の Container resource を返す。"""
    context = Context.from_toml(stage=stage)
    if not context.container:
        raise click.ClickException("container is not configured for this stage")
    try:
        name = resolve_container_name(context, container_name)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from e
    return Container(context.container[name])


_container_option = click.option(
    "--container",
    "container_name",
    default=None,
    help="対象 container 名 (1 つだけなら省略可)",
)


@container.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@_container_option
def yaml(stage, container_name):
    c = get_container_resource(stage, container_name)
    print(c.stack.yaml)


@container.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@_container_option
def yaml_diff(stage, container_name):
    c = get_container_resource(stage, container_name)
    print(c.stack.yaml_diff.to_json(indent=2))


@container.group()
def secrets():
    pass


def _secrets_views(context: Context):
    """(ラベル, SecretsContext) の一覧 (shared store + 各 container store)。"""
    views = []
    if context.secrets:
        views.append(("shared", context.secrets))
    for c_name in sorted(context.container):
        sc = context.container[c_name].secrets
        if sc:
            views.append((c_name, sc))
    return views


@secrets.command("list")
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--show-values", is_flag=True, default=False)
def list_secrets(stage, show_values):
    context = Context.from_toml(stage=stage)
    views = _secrets_views(context)
    if not views:
        echo.warning("secrets is not configured for this stage")
        return
    for label, sc in views:
        echo.info("[%s] %s" % (label, sc.pocket_key))
        for key, spec in sc.user.items():
            effective_store = spec.store or sc.store
            print("%s: %s (store=%s)" % (key, spec.name, effective_store))
            if show_values:
                if effective_store == "sm":
                    client = boto3.client("secretsmanager", region_name=sc.region)
                    value = client.get_secret_value(SecretId=spec.name)["SecretString"]
                else:
                    client = boto3.client("ssm", region_name=sc.region)
                    value = client.get_parameter(Name=spec.name, WithDecryption=True)[
                        "Parameter"
                    ]["Value"]
                print("  - " + value)
        for key, pocket_secret in sc.managed.items():
            status = "CREATED" if key in sc.pocket_store.secrets else "NOEXIST"
            print("%s: %s %s" % (key, pocket_secret.type, pocket_secret.options))
            print("  - " + status)
            if (status == "CREATED") and show_values:
                value = sc.pocket_store.secrets[key]
                if isinstance(value, str):
                    print("  - " + value)
                else:
                    for k, v in value.items():
                        print(f"  - {k}: {v}")


@secrets.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def create_pocket_managed(stage):
    context = Context.from_toml(stage=stage)
    if not _secrets_views(context):
        echo.warning("secrets is not configured for this stage")
        return
    mediator = Mediator(context)
    mediator.create_pocket_managed_secrets()


def _confirm_delete_pocket_managed_secrets(views):
    existing_secret_keys = [
        "%s (%s)" % (key, label)
        for label, sc in views
        for key in sc.managed.keys()
        if key in sc.pocket_store.secrets
    ]
    if not existing_secret_keys:
        echo.warning("No pocket managed secets are created yet.")
        return
    echo.warning("You are deleting pocket managed secrets.")
    echo.info("Deleting secrets:")
    for key in existing_secret_keys:
        echo.info(" - " + key)
    echo.danger("This data cannot be restored!")
    click.confirm("Do you realy want to delete pocket managed secrets?", abort=True)


@secrets.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def delete_pocket_managed(stage):
    context = Context.from_toml(stage=stage)
    views = _secrets_views(context)
    if not views:
        echo.warning("secrets is not configured")
        return
    _confirm_delete_pocket_managed_secrets(views)
    for _label, sc in views:
        sc.pocket_store.delete_secrets()


@container.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@_container_option
def create(stage, container_name):
    c = get_container_resource(stage, container_name)
    if not c.status == "NOEXIST":
        echo.warning("AWS lambda container is already created.")
    else:
        mediator = Mediator(Context.from_toml(stage=stage))
        c.create(mediator)
        echo.success("Created: lambda")


@container.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--with-secrets", is_flag=True, default=False)
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="確認プロンプトをスキップ"
)
def destroy(stage, with_secrets, yes):
    """Container 関連リソース (全 container) を削除する。

    トップレベル `pocket destroy` と同じ実装を使う (共有 ECR の削除ガード /
    stack 削除完了待ち / CodeBuild / log group 掃除を含む)。
    """
    context = Context.from_toml(stage=stage)
    if not context.container:
        echo.warning("container is not configured for this stage")
        return
    targets = _collect_container_targets(context, with_secrets)
    if not targets:
        echo.warning("削除対象のリソースが見つかりません。")
        return
    echo.danger("以下のリソースを削除します:")
    for target in targets:
        echo.info("  - %s" % target)
    echo.danger("この操作は取り消せません！")
    if not yes:
        click.confirm(
            "stage '%s' の Container リソースを削除しますか？" % stage, abort=True
        )
    _destroy_containers(context, with_secrets)


@container.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@_container_option
def update(stage, container_name):
    c = get_container_resource(stage, container_name)
    if c.status == "NOEXIST":
        echo.warning("AWS lambda has not created yet.")
        return
    if c.status == "FAILED":
        echo.danger("AWS lambda has failed. Please check console.")
        return
    if c.status == "PROGRESS":
        echo.warning("AWS lambda is updating. Please wait.")
        return
    mediator = Mediator(Context.from_toml(stage=stage))
    c.update(mediator)


@container.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@_container_option
def status(stage, container_name):
    c = get_container_resource(stage, container_name)
    if c.status == "COMPLETED":
        echo.success("Container is working!!!")
    elif c.status == "NOEXIST":
        echo.warning("Container has not created yet.")
    elif c.status == "FAILED":
        echo.danger("Container has failed. Please check console.")
    else:
        echo.warning("Container stack status: %s" % c.stack.status)


def _resolve_lambda_target_handlers(c_ctx, handler_name: str | None) -> list[str]:
    """reload-env / status-env の対象 handler を解決する。"""
    handlers = c_ctx.handlers
    if handler_name:
        if handler_name not in handlers:
            raise click.ClickException(
                "handler '%s' が見つかりません。利用可能: %s"
                % (handler_name, ", ".join(sorted(handlers.keys())))
            )
        return [handler_name]
    return list(handlers.keys())


def _fetch_lambda_env(client, function_name: str) -> dict[str, str]:
    """Lambda の現状 Environment.Variables を取得する。"""
    try:
        config = client.get_function_configuration(FunctionName=function_name)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            raise click.ClickException(
                "Lambda function '%s' が見つかりません。先に `pocket deploy` を"
                "実行してください。" % function_name
            ) from e
        raise
    return dict(config.get("Environment", {}).get("Variables", {}))


@container.command("reload-env")
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@_container_option
@click.option(
    "--handler", default=None, help="特定 handler のみ対象 (省略時は全 handler)"
)
def reload_env(stage, container_name, handler):
    """SSM/Secrets Manager の最新値で Lambda env を即時更新する (CFn を介さない)。

    deploy 時の CFn snapshot を base に、secrets (managed + user) の最新値を
    boto3 で取得して上書きし、`update_function_configuration` で Lambda に反映。
    side-channel update なので container 再生成が即座に走り、warm container
    内の古い os.environ もリセットされる。

    設計思想は `pocket waf ip` と同じ (CFn template は deploy 時 snapshot、
    実体は CLI で直接更新、次 deploy で自己治癒)。
    """
    context = Context.from_toml(stage=stage)
    if not context.container:
        raise click.ClickException("[container.<name>] が設定されていません")
    try:
        c_name = resolve_container_name(context, container_name)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from e
    c_ctx = context.container[c_name]

    fresh_secrets = get_secrets(stage, container=c_name)
    if not fresh_secrets:
        echo.warning("secrets が宣言されていません。何もしません。")
        return

    lambda_client = boto3.client("lambda", region_name=c_ctx.region)
    targets = _resolve_lambda_target_handlers(c_ctx, handler)

    for h_name in targets:
        function_name = c_ctx.handlers[h_name].function_name
        current = _fetch_lambda_env(lambda_client, function_name)
        new_env = {**current, **fresh_secrets}
        if new_env == current:
            echo.info("[%s] 差分なし (handler 内 env は既に最新)" % h_name)
            continue
        changed = sorted(k for k in fresh_secrets if current.get(k) != fresh_secrets[k])
        lambda_client.update_function_configuration(
            FunctionName=function_name,
            Environment={"Variables": new_env},
        )
        echo.success(
            "[%s] env を更新しました (%d/%d 秘密値を反映、warm container は再生成)"
            % (h_name, len(changed), len(fresh_secrets))
        )
        for k in changed:
            echo.log("  - %s" % k)


@container.command("status-env")
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@_container_option
@click.option(
    "--handler", default=None, help="特定 handler のみ対象 (省略時は全 handler)"
)
def status_env(stage, container_name, handler):
    """Lambda の現在 env と SSM/SM 上の宣言値の drift を表示する。"""
    context = Context.from_toml(stage=stage)
    if not context.container:
        raise click.ClickException("[container.<name>] が設定されていません")
    try:
        c_name = resolve_container_name(context, container_name)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from e
    c_ctx = context.container[c_name]

    fresh_secrets = get_secrets(stage, container=c_name)
    lambda_client = boto3.client("lambda", region_name=c_ctx.region)
    targets = _resolve_lambda_target_handlers(c_ctx, handler)

    any_drift = False
    for h_name in targets:
        function_name = c_ctx.handlers[h_name].function_name
        current = _fetch_lambda_env(lambda_client, function_name)
        drift = [k for k in fresh_secrets if current.get(k) != fresh_secrets[k]]
        echo.info(
            "[%s] secret keys: %d declared, drift: %d"
            % (h_name, len(fresh_secrets), len(drift))
        )
        for k in sorted(drift):
            if k not in current:
                echo.warning("  + %s (Lambda に未反映、reload-env で投入)" % k)
            else:
                echo.warning("  ~ %s (Lambda 値が古い、reload-env で更新)" % k)
        if drift:
            any_drift = True
    if any_drift:
        echo.warning(
            "drift があります。`pocket resource container reload-env` で同期できます。"
        )
    else:
        echo.success("drift なし。Lambda env と secrets は同期されています。")


@container.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@_container_option
@click.option("--openpath")
def url(stage, container_name, openpath):
    c = get_container_resource(stage, container_name)
    if c.status == "COMPLETED":
        if endpoint := c.endpoints.get("wsgi"):
            echo.success(f"wsgi url: {endpoint}")
            if openpath:
                webbrowser.open(endpoint + "/" + openpath)
        else:
            echo.warning("wsgi endpoint not found.")
    else:
        echo.warning("Container is not working.")
