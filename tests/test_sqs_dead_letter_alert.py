"""SQS DLQ アラート (sqs.dead_letter_alert) のテスト (KN1110)。

- 宣言必須 (未宣言は設定ロード時に fail-loud)
- enabled = false は「意図した無監視」として通知リソースを作らず通る
- email 宣言時は SNS Topic / Subscription / CloudWatch Alarm が生成される
- deploy 後の購読状態チェック (PendingConfirmation は警告)
"""

from __future__ import annotations

from unittest import mock

import boto3
import pytest
import yaml
from moto import mock_aws
from pocket_cli.resources.aws.cloudformation import ContainerStack
from pocket_cli.resources.sqs_alert import (
    dead_letter_alert_statuses,
    echo_dead_letter_alert_warnings,
)
from pydantic import ValidationError

from pocket import settings
from pocket.context import Context

_TOML_TEMPLATE = """
[general]
region = "ap-northeast-1"
project_name = "testprj"
stages = ["dev"]

[container.main]
dockerfile_path = "Dockerfile"

[container.main.handlers.worker]
command = "app.worker_handler"
timeout = 60
%s
"""


def _write_toml(tmp_path, sqs_line: str):
    toml_path = tmp_path / "pocket.toml"
    toml_path.write_text(_TOML_TEMPLATE % sqs_line)
    return str(toml_path)


def _context(use_toml, tmp_path, sqs_line: str) -> Context:
    use_toml(_write_toml(tmp_path, sqs_line))
    return Context.from_toml(stage="dev")


# ---------------------------------------------------------------------------
# settings: 宣言必須の validator
# ---------------------------------------------------------------------------


def test_sqs_without_dead_letter_alert_is_rejected():
    with pytest.raises(ValidationError, match="dead_letter_alert is required"):
        settings.Sqs.model_validate({})


def test_sqs_without_dead_letter_alert_error_contains_toml_hint():
    """エラーメッセージだけで直せるよう、貼れる TOML 雛形を含む"""
    with pytest.raises(ValidationError, match='dead_letter_alert = { email = "'):
        settings.Sqs.model_validate({})


def test_dead_letter_alert_with_email_is_accepted():
    sqs = settings.Sqs.model_validate(
        {"dead_letter_alert": {"email": "ops@example.com"}}
    )
    assert sqs.dead_letter_alert
    assert sqs.dead_letter_alert.enabled
    assert sqs.dead_letter_alert.email == "ops@example.com"


def test_dead_letter_alert_disabled_is_accepted_without_email():
    sqs = settings.Sqs.model_validate({"dead_letter_alert": {"enabled": False}})
    assert sqs.dead_letter_alert
    assert not sqs.dead_letter_alert.enabled


def test_dead_letter_alert_enabled_without_email_is_rejected():
    with pytest.raises(ValidationError, match="requires email"):
        settings.Sqs.model_validate({"dead_letter_alert": {}})


def test_dead_letter_alert_rejects_unknown_keys():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        settings.Sqs.model_validate({"dead_letter_alert": {"emai": "ops@example.com"}})


# ---------------------------------------------------------------------------
# context: naming と enabled=false の正規化
# ---------------------------------------------------------------------------


@mock_aws
def test_context_builds_alert_naming(use_toml, tmp_path):
    context = _context(
        use_toml,
        tmp_path,
        'sqs = { dead_letter_alert = { email = "ops@example.com" } }',
    )
    sqs_ctx = context.container["main"].handlers["worker"].sqs
    assert sqs_ctx and sqs_ctx.dead_letter_alert
    assert sqs_ctx.dead_letter_alert.email == "ops@example.com"
    assert sqs_ctx.dead_letter_alert.topic_name == (sqs_ctx.name + "-dead-letter-alert")
    assert sqs_ctx.dead_letter_alert.alarm_name == (sqs_ctx.name + "-dead-letter-alert")


@mock_aws
def test_context_normalizes_disabled_alert_to_none(use_toml, tmp_path):
    context = _context(
        use_toml, tmp_path, "sqs = { dead_letter_alert = { enabled = false } }"
    )
    sqs_ctx = context.container["main"].handlers["worker"].sqs
    assert sqs_ctx and sqs_ctx.dead_letter_alert is None


# ---------------------------------------------------------------------------
# CFn template: 通知リソースの生成
# ---------------------------------------------------------------------------


@mock_aws
def test_template_renders_alert_resources(use_toml, tmp_path):
    context = _context(
        use_toml,
        tmp_path,
        'sqs = { dead_letter_alert = { email = "ops@example.com" } }',
    )
    resources = yaml.safe_load(ContainerStack(context.container["main"]).yaml)[
        "Resources"
    ]
    topic = resources["WorkerDeadLetterAlertTopic"]
    subscription = resources["WorkerDeadLetterAlertSubscription"]
    alarm = resources["WorkerDeadLetterAlarm"]
    assert topic["Type"] == "AWS::SNS::Topic"
    assert subscription["Properties"]["Protocol"] == "email"
    assert subscription["Properties"]["Endpoint"] == "ops@example.com"
    assert alarm["Properties"]["Namespace"] == "AWS/SQS"
    assert alarm["Properties"]["MetricName"] == "ApproximateNumberOfMessagesVisible"
    assert alarm["Properties"]["Threshold"] == 1
    assert alarm["Properties"]["TreatMissingData"] == "notBreaching"
    # 復旧も通知する
    assert alarm["Properties"]["OKActions"] == alarm["Properties"]["AlarmActions"]


@mock_aws
def test_template_omits_alert_resources_when_disabled(use_toml, tmp_path):
    context = _context(
        use_toml, tmp_path, "sqs = { dead_letter_alert = { enabled = false } }"
    )
    resources = yaml.safe_load(ContainerStack(context.container["main"]).yaml)[
        "Resources"
    ]
    assert "WorkerSqsQueue" in resources
    assert "WorkerDeadLetterQueue" in resources
    assert "WorkerDeadLetterAlertTopic" not in resources
    assert "WorkerDeadLetterAlertSubscription" not in resources
    assert "WorkerDeadLetterAlarm" not in resources


# ---------------------------------------------------------------------------
# deploy 後の購読状態チェック
# ---------------------------------------------------------------------------


@mock_aws
def test_statuses_not_created_before_deploy(use_toml, tmp_path):
    context = _context(
        use_toml,
        tmp_path,
        'sqs = { dead_letter_alert = { email = "ops@example.com" } }',
    )
    statuses = dead_letter_alert_statuses(context)
    assert len(statuses) == 1
    assert statuses[0].state == "NotCreated"


@mock_aws
def test_statuses_confirmed_subscription(use_toml, tmp_path):
    context = _context(
        use_toml,
        tmp_path,
        'sqs = { dead_letter_alert = { email = "ops@example.com" } }',
    )
    sqs_ctx = context.container["main"].handlers["worker"].sqs
    assert sqs_ctx and sqs_ctx.dead_letter_alert
    alert = sqs_ctx.dead_letter_alert
    sns = boto3.client("sns", region_name="ap-northeast-1")
    topic_arn = sns.create_topic(Name=alert.topic_name)["TopicArn"]
    sns.subscribe(TopicArn=topic_arn, Protocol="email", Endpoint="ops@example.com")
    statuses = dead_letter_alert_statuses(context)
    assert [s.state for s in statuses] == ["Confirmed"]


@mock_aws
def test_statuses_empty_when_alert_disabled(use_toml, tmp_path):
    context = _context(
        use_toml, tmp_path, "sqs = { dead_letter_alert = { enabled = false } }"
    )
    assert dead_letter_alert_statuses(context) == []


@mock_aws
def test_pending_confirmation_warns(use_toml, tmp_path):
    """PendingConfirmation (確認メール未対応) は deploy 後に警告される"""
    context = _context(
        use_toml,
        tmp_path,
        'sqs = { dead_letter_alert = { email = "ops@example.com" } }',
    )
    pending = {
        "Subscriptions": [
            {
                "SubscriptionArn": "PendingConfirmation",
                "Protocol": "email",
                "Endpoint": "ops@example.com",
            }
        ]
    }
    with (
        mock.patch("pocket_cli.resources.sqs_alert.boto3.client") as client_factory,
        mock.patch("pocket.utils.echo.warning") as warning,
    ):
        client_factory.return_value.get_caller_identity.return_value = {
            "Account": "123456789012"
        }
        client_factory.return_value.list_subscriptions_by_topic.return_value = pending
        echo_dead_letter_alert_warnings(context)
    assert warning.called
    assert "PendingConfirmation" in warning.call_args[0][0]
