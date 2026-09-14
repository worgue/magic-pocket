import click

from pocket.context import Context
from pocket.resources.aws.secretsmanager import PocketSecretIsNotReady
from pocket.utils import echo
from pocket_cli.cli.deploy_cli import get_resources
from pocket_cli.resources.container import Container
from pocket_cli.resources.inbound import echo_inbound_details
from pocket_cli.resources.neon import Neon
from pocket_cli.resources.sqs_alert import echo_dead_letter_alert_statuses


def skip_reason(resource) -> str | None:
    """status を取らずに skip する理由 (無ければ None)。

    外部 provider の資格情報が無い環境 (例: NEON_API_KEY を持たない VM) でも、
    stack の状態確認に provider は無関係なので止めずに続行する (KN1456)。
    """
    if isinstance(resource, Neon) and not resource.has_credential:
        return (
            "Neon status: NEON_API_KEY が未設定のため状態確認をスキップします "
            "(stack の状態確認・削除に Neon の資格情報は不要です)"
        )
    return None


def show_status_message(resource):
    target_name = resource.__class__.__name__
    message = f"{target_name} status: {resource.status}"
    echo_fn = {
        "NOEXIST": echo.info,
        "REQUIRE_UPDATE": echo.warning,
        "PROGRESS": echo.warning,
        "COMPLETED": echo.success,
        "FAILED": echo.danger,
    }[resource.status]
    echo_fn(message)


def show_info_message(resource):
    if hasattr(resource, "description"):
        echo.info(resource.description)
    if isinstance(resource, Container) and resource.context.secrets_views():
        for sc in resource.context.secrets_views():
            if not sc.managed:
                continue
            try:
                _ = sc.pocket_store.arn
            except PocketSecretIsNotReady:
                echo.warning(
                    "Because pocket managed secrets is not ready yet, "
                    "the context is not ready to use."
                )
                echo.warning("Just use it for reference.")
                echo.info("You can create pocket managed secrets by running the below.")
                echo.info("pocket resource container secrets create-pocket-managed")

        try:
            _ = resource.context.allowed_sm_resources
        except PocketSecretIsNotReady:
            echo.warning("Please create pocket secrets first.")
            return
    print(resource.context.model_dump_json(indent=2))


@click.command()
@click.option("--show-info", is_flag=True, default=False)
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def status(stage, show_info):
    context = Context.from_toml(stage=stage)
    resources = get_resources(context)
    for resource in resources:
        reason = skip_reason(resource)
        if reason:
            echo.warning(reason)
            continue
        show_status_message(resource)
        if show_info:
            show_info_message(resource)
    # DLQ アラートの email 購読状態 (PendingConfirmation のままだと通知が届かない)
    echo_dead_letter_alert_statuses(context)
    echo_inbound_details(context)
