import json

import click

from pocket.context import Context
from pocket.resources.aws.secretsmanager import PocketSecretIsNotReady
from pocket.utils import echo, mask_secret_values
from pocket_cli.cli.deploy_cli import get_resources
from pocket_cli.mediator import Mediator
from pocket_cli.resources.cloudfront import CloudFront
from pocket_cli.resources.cloudfront_keys import CloudFrontKeys
from pocket_cli.resources.cloudfront_waf import CloudFrontWaf
from pocket_cli.resources.container import Container
from pocket_cli.resources.inbound import echo_inbound_details
from pocket_cli.resources.sqs_alert import echo_dead_letter_alert_statuses


def load_template_secrets(resource, mediator: Mediator):
    """template に入る secret の値を store から読む (読むだけで作らない)。

    WAF の allow secret・CloudFront keys の公開鍵・CloudFront の origin verify secret は
    template に焼き込まれるので、空のまま hash を計算すると deploy 済みの stack が
    REQUIRE_UPDATE に見える。deploy と同じ値で判定する。
    """
    if isinstance(resource, (CloudFrontWaf, CloudFrontKeys, CloudFront)):
        resource.prepare_deploy(mediator)


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
    # `pocket context` と同じく secret (API key 等) はマスクする
    print(
        json.dumps(
            mask_secret_values(resource.context.model_dump(mode="json")), indent=2
        )
    )


@click.command()
@click.option("--show-info", is_flag=True, default=False)
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def status(stage, show_info):
    context = Context.from_toml(stage=stage)
    resources = get_resources(context)
    mediator = Mediator(context)
    for resource in resources:
        load_template_secrets(resource, mediator)
        show_status_message(resource)
        if show_info:
            show_info_message(resource)
    # DLQ アラートの email 購読状態 (PendingConfirmation のままだと通知が届かない)
    echo_dead_letter_alert_statuses(context)
    echo_inbound_details(context)
