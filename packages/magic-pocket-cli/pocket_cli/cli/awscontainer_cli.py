from __future__ import annotations

import webbrowser

import boto3
import click
from botocore.exceptions import ClientError

from pocket.context import Context
from pocket.runtime import get_secrets
from pocket.utils import echo
from pocket_cli.cli.destroy_cli import (
    _collect_awscontainer_targets,
    _destroy_awscontainer,
)
from pocket_cli.cli.resource_helper import require_configured
from pocket_cli.mediator import Mediator
from pocket_cli.resources.awscontainer import AwsContainer


@click.group()
def awscontainer():
    pass


def get_awscontainer_resource(stage):
    context = Context.from_toml(stage=stage)
    return AwsContainer(
        context=require_configured(
            context.awscontainer, "awscontainer is not configured for this stage"
        )
    )


@awscontainer.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def yaml(stage):
    ac = get_awscontainer_resource(stage)
    print(ac.stack.yaml)


@awscontainer.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def yaml_diff(stage):
    ac = get_awscontainer_resource(stage)
    print(ac.stack.yaml_diff.to_json(indent=2))


@awscontainer.group()
def secrets():
    pass


@secrets.command("list")
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--show-values", is_flag=True, default=False)
def list_secrets(stage, show_values):
    ac = get_awscontainer_resource(stage)
    sc = ac.context.secrets
    if not sc:
        echo.warning("secrets is not configured for this stage")
        return
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
    ac = get_awscontainer_resource(stage)
    sc = ac.context.secrets
    if not sc:
        echo.warning("secrets is not configured for this stage")
        return
    mediator = Mediator(Context.from_toml(stage=stage))
    mediator.create_pocket_managed_secrets()


def _confirm_delete_pocket_managed_secrets(awscontainer: AwsContainer):
    sc = awscontainer.context.secrets
    if not sc:
        echo.warning("secrets is not configured")
        return
    existing_secret_keys = [
        key for key in sc.managed.keys() if key in sc.pocket_store.secrets
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
    ac = get_awscontainer_resource(stage)
    _confirm_delete_pocket_managed_secrets(ac)
    if ac.context.secrets:
        ac.context.secrets.pocket_store.delete_secrets()


@awscontainer.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def create(stage):
    ac = get_awscontainer_resource(stage)
    if not ac.status == "NOEXIST":
        echo.warning("AWS lambda container is already created.")
    else:
        mediator = Mediator(Context.from_toml(stage=stage))
        ac.create(mediator)
        echo.success("Created: lambda")


@awscontainer.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--with-secrets", is_flag=True, default=False)
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="確認プロンプトをスキップ"
)
def destroy(stage, with_secrets, yes):
    """AwsContainer 関連リソースを削除する。

    トップレベル `pocket destroy` と同じ実装を使う (共有 ECR の削除ガード /
    stack 削除完了待ち / CodeBuild / log group 掃除を含む)。
    """
    context = Context.from_toml(stage=stage)
    if not context.awscontainer:
        echo.warning("awscontainer is not configured for this stage")
        return
    targets = _collect_awscontainer_targets(context, with_secrets)
    if not targets:
        echo.warning("削除対象のリソースが見つかりません。")
        return
    echo.danger("以下のリソースを削除します:")
    for target in targets:
        echo.info("  - %s" % target)
    echo.danger("この操作は取り消せません！")
    if not yes:
        click.confirm(
            "stage '%s' の AwsContainer リソースを削除しますか？" % stage, abort=True
        )
    _destroy_awscontainer(context, with_secrets)


@awscontainer.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def update(stage):
    ac = get_awscontainer_resource(stage)
    if ac.status == "NOEXIST":
        echo.warning("AWS lambda has not created yet.")
        return
    if ac.status == "FAILED":
        echo.danger("AWS lambda has failed. Please check console.")
        return
    if ac.status == "PROGRESS":
        echo.warning("AWS lambda is updating. Please wait.")
        return
    mediator = Mediator(Context.from_toml(stage=stage))
    ac.update(mediator)


@awscontainer.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def status(stage):
    ac = get_awscontainer_resource(stage)
    if ac.status == "COMPLETED":
        echo.success("Container is working!!!")
    elif ac.status == "NOEXIST":
        echo.warning("Container has not created yet.")
    elif ac.status == "FAILED":
        echo.danger("Container has failed. Please check console.")
    else:
        echo.warning("Container stack status: %s" % ac.stack.status)


def _resolve_lambda_target_handlers(
    context: Context, handler_name: str | None
) -> list[str]:
    """reload-env / status-env の対象 handler を解決する。"""
    if context.awscontainer is None:
        raise RuntimeError("awscontainer is not configured")
    handlers = context.awscontainer.handlers
    if handler_name:
        if handler_name not in handlers:
            raise click.ClickException(
                "handler '%s' が見つかりません。利用可能: %s"
                % (handler_name, ", ".join(sorted(handlers.keys())))
            )
        return [handler_name]
    return list(handlers.keys())


def _function_name(context: Context, handler_key: str) -> str:
    if context.awscontainer is None:
        raise RuntimeError("awscontainer is not configured")
    # deploy 側 (LambdaHandlerContext.function_name = resource_prefix + key) と同じ
    # 正準名を参照する。slug から再構成すると prefix_template / namespace
    # (既定 `pocket`) を取りこぼすため、handler context の値をそのまま使う。
    return context.awscontainer.handlers[handler_key].function_name


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


@awscontainer.command("reload-env")
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option(
    "--handler", default=None, help="特定 handler のみ対象 (省略時は全 handler)"
)
def reload_env(stage, handler):
    """SSM/Secrets Manager の最新値で Lambda env を即時更新する (CFn を介さない)。

    deploy 時の CFn snapshot を base に、secrets (managed + user) の最新値を
    boto3 で取得して上書きし、`update_function_configuration` で Lambda に反映。
    side-channel update なので container 再生成が即座に走り、warm container
    内の古い os.environ もリセットされる。

    設計思想は `pocket waf ip` と同じ (CFn template は deploy 時 snapshot、
    実体は CLI で直接更新、次 deploy で自己治癒)。
    """
    context = Context.from_toml(stage=stage)
    if not context.awscontainer:
        raise click.ClickException("[awscontainer] が設定されていません")

    fresh_secrets = get_secrets(stage)
    if not fresh_secrets:
        echo.warning("secrets が宣言されていません。何もしません。")
        return

    lambda_client = boto3.client("lambda", region_name=context.awscontainer.region)
    targets = _resolve_lambda_target_handlers(context, handler)

    for h_name in targets:
        function_name = _function_name(context, h_name)
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


@awscontainer.command("status-env")
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option(
    "--handler", default=None, help="特定 handler のみ対象 (省略時は全 handler)"
)
def status_env(stage, handler):
    """Lambda の現在 env と SSM/SM 上の宣言値の drift を表示する。"""
    context = Context.from_toml(stage=stage)
    if not context.awscontainer:
        raise click.ClickException("[awscontainer] が設定されていません")

    fresh_secrets = get_secrets(stage)
    lambda_client = boto3.client("lambda", region_name=context.awscontainer.region)
    targets = _resolve_lambda_target_handlers(context, handler)

    any_drift = False
    for h_name in targets:
        function_name = _function_name(context, h_name)
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
            "drift があります。"
            "`pocket resource awscontainer reload-env` で同期できます。"
        )
    else:
        echo.success("drift なし。Lambda env と secrets は同期されています。")


@awscontainer.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--openpath")
def url(stage, openpath):
    ac = get_awscontainer_resource(stage)
    if ac.status == "COMPLETED":
        if endpoint := ac.endpoints.get("wsgi"):
            echo.success(f"wsgi url: {endpoint}")
            if openpath:
                webbrowser.open(endpoint + "/" + openpath)
        else:
            echo.warning("wsgi endpoint not found.")
    else:
        echo.warning("Container is not working.")
