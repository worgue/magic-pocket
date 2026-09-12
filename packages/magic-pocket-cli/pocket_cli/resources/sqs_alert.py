"""SQS DLQ アラートの SNS email 購読状態の照会。

SNS の email 購読は、宛先が確認メールのリンクを踏むまで ``PendingConfirmation``
のままで、その間はアラームが鳴っても通知が届かない (「監視してあるつもり」の
無監視)。deploy の最後と ``pocket status`` で購読状態を可視化し、未確認のまま
放置されるのを防ぐ (KN1110)。
"""

from __future__ import annotations

from typing import Literal, NamedTuple

import boto3

from pocket.context import Context
from pocket.utils import echo

SubscriptionState = Literal["Confirmed", "PendingConfirmation", "Deleted", "NotCreated"]


class DeadLetterAlertStatus(NamedTuple):
    topic_name: str
    email: str
    state: SubscriptionState


def dead_letter_alert_statuses(context: Context) -> list[DeadLetterAlertStatus]:
    """dead_letter_alert が enabled な handler ごとの email 購読状態を返す。

    topic 未作成 (deploy 前 / 削除済み) は ``NotCreated``。購読が確認済みなら
    ``Confirmed``、確認メール未対応なら ``PendingConfirmation``、unsubscribe 済みなら
    ``Deleted``。SNS の ``SubscriptionArn`` は実 ARN のほかに特殊値
    (``PendingConfirmation`` / ``Deleted``) を取るため、実 ARN (``arn:`` 始まり) の
    ときだけ ``Confirmed`` と判定する。同一 email の購読が複数並ぶ場合
    (unsubscribe 後の再購読など) は良い方の状態を採る。
    """
    targets = [
        (c.region, h.sqs.dead_letter_alert.topic_name, h.sqs.dead_letter_alert.email)
        for c in context.container.values()
        for h in c.handlers.values()
        if h.sqs and h.sqs.dead_letter_alert and h.sqs.dead_letter_alert.email
    ]
    targets.extend(
        (
            inlet.region,
            inlet.resource_name + "-alert",
            inlet.config.delivery_alert.email,
        )
        for inlet in context.inbound.values()
        if inlet.config.delivery_alert.enabled and inlet.config.delivery_alert.email
    )
    if not targets:
        return []
    account_id = boto3.client("sts").get_caller_identity()["Account"]
    statuses: list[DeadLetterAlertStatus] = []
    for region, topic_name, email in targets:
        sns = boto3.client("sns", region_name=region)
        topic_arn = f"arn:aws:sns:{region}:{account_id}:{topic_name}"
        try:
            res = sns.list_subscriptions_by_topic(TopicArn=topic_arn)
        except sns.exceptions.NotFoundException:
            statuses.append(DeadLetterAlertStatus(topic_name, email, "NotCreated"))
            continue
        state: SubscriptionState = "NotCreated"
        for sub in res["Subscriptions"]:
            if sub["Protocol"] != "email" or sub["Endpoint"] != email:
                continue
            arn = sub["SubscriptionArn"]
            if arn.startswith("arn:"):
                state = "Confirmed"
                break
            if arn == "PendingConfirmation":
                state = "PendingConfirmation"
            elif state == "NotCreated":
                state = "Deleted"
        statuses.append(DeadLetterAlertStatus(topic_name, email, state))
    return statuses


def echo_dead_letter_alert_warnings(context: Context) -> None:
    """未確認の購読を警告する (deploy の最後に呼ぶ)。

    初回 deploy は宛先が確認メールを踏むまで必ず ``PendingConfirmation`` に
    なるため、エラーではなく警告に留める (エラーにすると初回 deploy が
    必ず落ちる)。確認済みなら何も出さない。
    """
    for status in dead_letter_alert_statuses(context):
        if status.state == "Confirmed":
            continue
        if status.state == "PendingConfirmation":
            echo.warning(
                f"DLQ alert subscription for {status.email} ({status.topic_name}) "
                "is still PendingConfirmation. Alarm notifications will NOT be "
                "delivered until the confirmation link in the email is clicked."
            )
        elif status.state == "Deleted":
            echo.warning(
                f"DLQ alert subscription for {status.email} ({status.topic_name}) "
                "has been unsubscribed (SubscriptionArn=Deleted). Alarm "
                "notifications will NOT be delivered. Re-subscribe the email and "
                "confirm it again."
            )
        else:
            echo.warning(
                f"DLQ alert subscription for {status.email} ({status.topic_name}) "
                "was not found. Check the container stack deployment."
            )


def echo_dead_letter_alert_statuses(context: Context) -> None:
    """購読状態を一覧表示する (``pocket status`` 用)。"""
    for status in dead_letter_alert_statuses(context):
        message = f"DLQ alert {status.topic_name} -> {status.email}: {status.state}"
        if status.state == "Confirmed":
            echo.success(message)
        else:
            echo.warning(message)
