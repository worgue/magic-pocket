"""受信口の設定、AWS接続、安全な保存・取り込みの回帰テスト。"""

import json
from unittest import mock

import boto3
import pytest
import yaml
from click.testing import CliRunner
from moto import mock_aws
from pocket_cli.cli.inbound_cli import inbound
from pocket_cli.resources.aws.cloudformation import ContainerStack
from pocket_cli.resources.inbound import Inbound, resolve_rules
from pocket_cli.resources.inbound_template import build_template
from pydantic import ValidationError

from pocket.context import Context
from pocket.inbound import Receiver, handler, receipt_id
from pocket.inbound_context import InboundContext
from pocket.permissions import compute_actions
from pocket.settings import Settings


@pytest.fixture
def receiving_settings():
    return Settings.model_validate(
        {
            "stage": "dev",
            "general": {
                "project_name": "receiving",
                "region": "ap-northeast-1",
                "stages": ["dev"],
            },
            "container": {
                "mail": {
                    "dockerfile_path": "Dockerfile",
                    "permissions_boundary": (
                        "arn:aws:iam::123456789012:policy/test-boundary"
                    ),
                    "handlers": {
                        "worker": {
                            "command": "app.mail.worker",
                            "sqs": {"dead_letter_alert": {"email": "ops@example.com"}},
                        }
                    },
                }
            },
            "inbound": {
                "inbox": {
                    "domain": "receive.example.com",
                    "recipients": ["test@receive.example.com"],
                    "handler": "mail.worker",
                    "retention_days": 30,
                    "delivery_alert": {"email": "ops@example.com"},
                }
            },
        }
    )


@pytest.fixture
def inlet(receiving_settings):
    return InboundContext.from_settings("inbox", receiving_settings)


def test_config_rejects_invalid_handlers_and_addresses(receiving_settings):
    for field, value in (
        ("handler", "mail.missing"),
        ("recipients", ["*@receive.example.com"]),
        ("retention_days", 13),
        ("metadata_prefix", "raw/metadata/"),
    ):
        data = receiving_settings.model_dump(exclude_computed_fields=True)
        data["inbound"]["inbox"][field] = value
        with pytest.raises(ValidationError):
            Settings.model_validate(data)


def test_inbound_permissions_separate_bootstrap(receiving_settings):
    actions = compute_actions(receiving_settings)
    assert "ses:CreateReceiptRule" in actions
    assert "ses:SetActiveReceiptRuleSet" not in actions
    assert "ses:SendRawEmail" not in actions


def test_region_and_name_isolation(receiving_settings, inlet):
    assert len(inlet.bucket_name.replace("${AWS::AccountId}", "123456789012")) <= 63
    receiving_settings.general.region = "us-east-1"
    assert (
        InboundContext.from_settings("inbox", receiving_settings).bucket_name
        != inlet.bucket_name
    )
    receiving_settings.general.region = "ap-south-2"
    with pytest.raises(ValueError, match="リージョン"):
        InboundContext.from_settings("inbox", receiving_settings)


def test_rule_tail_and_binding(inlet):
    active = {
        "Metadata": {"Name": "shared"},
        "Rules": [
            {
                "Name": "other",
                "Enabled": True,
                "Recipients": ["other@example.com"],
                "Actions": [],
            }
        ],
    }
    assert resolve_rules(active, inlet, None) == ("shared", "other")
    active["Rules"].append({"Name": inlet.resource_name, "Enabled": True})
    assert resolve_rules(
        active, inlet, {"RuleSet": "shared", "AfterRule": "other"}
    ) == ("shared", "other")
    with pytest.raises(ValueError, match="未管理"):
        resolve_rules(active, inlet, None)
    with pytest.raises(ValueError, match="変わって"):
        resolve_rules(active, inlet, {"RuleSet": "old"})


@pytest.mark.parametrize(
    "recipients,actions",
    [
        ([], []),
        (["receive.example.com"], []),
        ([".example.com"], []),
        (["test@receive.example.com"], []),
        (["other@example.com"], [{"StopAction": {}}]),
        (["other@example.com"], [{"BounceAction": {}}]),
        (["other@example.com"], [{"LambdaAction": {}}]),
    ],
)
def test_conflicting_rules_rejected(inlet, recipients, actions):
    with pytest.raises(ValueError, match="競合"):
        resolve_rules(
            {
                "Metadata": {"Name": "shared"},
                "Rules": [
                    {
                        "Name": "existing",
                        "Enabled": True,
                        "Recipients": recipients,
                        "Actions": actions,
                    }
                ],
            },
            inlet,
            None,
        )


def test_no_active_and_missing_rule_rejected(inlet):
    with pytest.raises(ValueError, match="init"):
        resolve_rules({}, inlet, None)
    with pytest.raises(ValueError, match="見つかりません"):
        resolve_rules({"Metadata": {"Name": "shared"}}, inlet, {"RuleSet": "shared"})


def test_template_security_and_delivery(inlet):
    template = build_template(inlet, "shared", "existing")
    resources = template["Resources"]
    rule = resources["ReceiptRule"]
    assert rule["Properties"]["After"] == "existing"
    assert "Subscription" in rule["DependsOn"]
    assert "BucketPolicy" in rule["DependsOn"]
    assert len(rule["Properties"]["Rule"]["Actions"]) == 1
    assert "S3Action" in rule["Properties"]["Rule"]["Actions"][0]
    assert resources["Bucket"]["DeletionPolicy"] == "Retain"
    assert (
        resources["Bucket"]["Properties"]["VersioningConfiguration"]["Status"]
        == "Enabled"
    )
    assert "NotificationConfiguration" not in resources["Bucket"]["Properties"]
    assert resources["Subscription"]["Properties"]["RawMessageDelivery"] is False
    assert resources["Subscription"]["Properties"]["RedrivePolicy"][
        "deadLetterTargetArn"
    ] == {"Fn::GetAtt": ["DeliveryDLQ", "Arn"]}
    ses_policy = resources["BucketPolicy"]["Properties"]["PolicyDocument"]["Statement"][
        0
    ]
    assert ses_policy["Condition"]["ArnEquals"]["AWS:SourceArn"]["Fn::Sub"].endswith(
        "receipt-rule/" + inlet.resource_name
    )
    assert resources["PublishFailureAlarm"]["Properties"]["Dimensions"] == [
        {"Name": "RuleName", "Value": inlet.resource_name}
    ]
    assert "AlarmActions" in resources["PublishFailureAlarm"]["Properties"]


@mock_aws
def test_container_runtime_config_and_eventbridge(receiving_settings):
    receiving_settings.inbound["inbox"].delivery_alert = receiving_settings.inbound[
        "inbox"
    ].delivery_alert.model_validate({"eventbridge": True})
    worker = receiving_settings.container["mail"].handlers["worker"]
    assert worker.sqs and worker.sqs.dead_letter_alert
    worker.sqs.dead_letter_alert = worker.sqs.dead_letter_alert.model_validate(
        {"eventbridge": True}
    )
    context = Context.from_settings(receiving_settings)
    container = yaml.safe_load(ContainerStack(context.container["mail"]).yaml)
    resources = container["Resources"]
    env = resources["WorkerLambdaFunction"]["Properties"]["Environment"]["Variables"]
    payload = env["POCKET_INBOUND"]["Fn::Sub"]
    assert (
        json.loads(payload)["inbox"]["bucket"] == context.inbound["inbox"].bucket_name
    )
    assert resources["WorkerSqsQueue"]["DeletionPolicy"] == "Retain"
    assert (
        resources["WorkerSqsQueue"]["Properties"]["MessageRetentionPeriod"] == "1209600"
    )
    assert "WorkerDeadLetterAlarm" in resources
    assert "WorkerDeadLetterAlertTopic" not in resources
    assert "AlarmActions" not in resources["WorkerDeadLetterAlarm"]["Properties"]
    inlet_resources = build_template(context.inbound["inbox"], "shared", None)[
        "Resources"
    ]
    assert "AlertTopic" not in inlet_resources
    assert "AlarmActions" not in inlet_resources["PublishExpiredAlarm"]["Properties"]


@pytest.fixture
def storage():
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        bucket = "inbound-test-bucket"
        s3.create_bucket(Bucket=bucket)
        s3.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        raw = (
            b"From: sender@example.com\r\nTo: different@example.com\r\n"
            b"Subject: test\r\n\r\nbody"
        )
        s3.put_object(Bucket=bucket, Key="raw/message", Body=raw)
        config = {
            "region": "us-east-1",
            "bucket": bucket,
            "topic_arn": "arn:aws:sns:us-east-1:123456789012:inbound",
            "raw_prefix": "raw/",
            "metadata_prefix": "metadata/",
            "import_prefix": "imports/",
            "recipients": ["test@receive.example.com"],
        }
        notification = {
            "notificationType": "Received",
            "mail": {
                "source": "sender@example.com",
                "messageId": "message",
                "destination": ["different@example.com"],
            },
            "receipt": {
                "recipients": config["recipients"],
                "spamVerdict": {"status": "FAIL"},
                "action": {
                    "type": "S3",
                    "bucketName": bucket,
                    "objectKey": "raw/message",
                    "topicArn": config["topic_arn"],
                },
            },
            "futureField": {"preserve": True},
        }
        envelope = {
            "Type": "Notification",
            "TopicArn": config["topic_arn"],
            "MessageId": "sns-1",
            "Message": json.dumps(notification),
        }
        record = {"messageId": "sqs-1", "body": json.dumps(envelope)}
        yield s3, config, record, raw


def test_preserve_full_notification_before_parse_and_retries(storage):
    s3, config, record, raw = storage
    receiver = Receiver("inbox", config=config, s3=s3)
    received = receiver.receive(record)
    assert received.raw == raw
    assert received.metadata["ses"]["receipt"]["spamVerdict"]["status"] == "FAIL"
    assert received.metadata["ses"]["futureField"] == {"preserve": True}
    assert received.metadata["ses"]["mail"]["destination"] != config["recipients"]
    assert received.parse()["Subject"] == "test"
    # 原本のcurrent versionが変わっても初回通知に結び付いた版を読む。
    s3.put_object(Bucket=config["bucket"], Key="raw/message", Body=b"replacement")
    assert receiver.receive(record).raw == raw
    versions = s3.list_object_versions(Bucket=config["bucket"], Prefix="metadata/")[
        "Versions"
    ]
    assert len(versions) == 1


def test_unexpected_notification_cannot_read_other_bucket(storage):
    s3, config, record, _ = storage
    envelope = json.loads(record["body"])
    envelope["TopicArn"] = "other"
    record["body"] = json.dumps(envelope)
    with mock.patch.object(s3, "get_object") as get:
        with pytest.raises(ValueError, match="想定外"):
            Receiver("inbox", config=config, s3=s3).receive(record)
        get.assert_not_called()


def test_partial_batch_failure_and_metadata_survives_processing_failure(
    storage, monkeypatch
):
    s3, config, record, _ = storage
    monkeypatch.setenv("POCKET_INBOUND", json.dumps({"inbox": config}))
    calls = []

    def process(mail):
        calls.append(mail.id)
        raise ValueError("解析失敗")

    result = handler("inbox", process)({"Records": [record]}, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": "sqs-1"}]}
    assert calls == [receipt_id(config["bucket"], "raw/message")]
    assert (
        s3.list_objects_v2(Bucket=config["bucket"], Prefix="metadata/")["KeyCount"] == 1
    )


def test_import_requires_complete_pair_and_hash(storage):
    s3, config, record, raw = storage
    receiver = Receiver("inbox", config=config, s3=s3)
    mail = receiver.receive(record)
    prefix = "imports/" + mail.id + "/"
    s3.put_object(
        Bucket=config["bucket"],
        Key=prefix + "manifest.json",
        Body=json.dumps({"receipt_id": mail.id}).encode(),
    )
    s3.put_object(Bucket=config["bucket"], Key=prefix + "raw.eml", Body=raw)
    with pytest.raises(ValueError, match="一致"):
        receiver.load_import(prefix + "manifest.json")
    s3.put_object(
        Bucket=config["bucket"],
        Key=prefix + "metadata.json",
        Body=json.dumps(mail.metadata).encode(),
    )
    assert receiver.load_import(prefix + "manifest.json").raw == raw
    s3.put_object(Bucket=config["bucket"], Key=prefix + "raw.eml", Body=b"corrupt")
    with pytest.raises(ValueError, match="一致"):
        receiver.load_import(prefix + "manifest.json")


@mock_aws
def test_preflight_refuses_handler_change_before_deploy(inlet):
    resource = Inbound(inlet)
    output = {"RuleSet": "shared", "Handler": "other.worker"}
    with (
        mock.patch.object(
            type(resource.stack),
            "output",
            new_callable=mock.PropertyMock,
            return_value=output,
        ),
        mock.patch.object(resource.stack, "binding", return_value=("shared", None)),
    ):
        boto3.client("ses", region_name=inlet.region).verify_domain_identity(
            Domain=inlet.config.domain
        )
        with pytest.raises(ValueError, match="Handler"):
            resource.prepare_deploy()


def test_cli_init_keeps_existing_active_set(receiving_settings, inlet):
    ses = mock.Mock()
    ses.describe_active_receipt_rule_set.return_value = {
        "Metadata": {"Name": "existing"}
    }
    ses.get_identity_verification_attributes.return_value = {
        "VerificationAttributes": {
            inlet.config.domain: {"VerificationStatus": "Success"}
        }
    }
    with (
        mock.patch(
            "pocket_cli.cli.inbound_cli.Settings.from_toml",
            return_value=receiving_settings,
        ),
        mock.patch("pocket_cli.cli.inbound_cli.boto3.client", return_value=ses),
    ):
        result = CliRunner().invoke(
            inbound, ["--stage", "dev", "--name", "inbox", "init"]
        )
    assert result.exit_code == 0, result.output
    ses.set_active_receipt_rule_set.assert_not_called()
    ses.create_receipt_rule_set.assert_not_called()
    assert "MX receive.example.com" in result.output


def test_existing_position_survives_predecessor_removal(inlet):
    active = {
        "Metadata": {"Name": "shared"},
        "Rules": [
            {"Name": inlet.resource_name, "Enabled": True},
            {"Name": "later", "Enabled": False},
        ],
    }
    assert resolve_rules(
        active, inlet, {"RuleSet": "shared", "AfterRule": "removed"}
    ) == ("shared", None)


@mock_aws
def test_runtime_context_does_not_require_deploy_boundary(
    receiving_settings, monkeypatch
):
    monkeypatch.delenv("FORGE_PERMISSIONS_BOUNDARY_ARN", raising=False)
    monkeypatch.delenv("POCKET_PERMISSIONS_BOUNDARY_ARN", raising=False)
    receiving_settings.container["mail"].permissions_boundary = None
    context = Context.from_settings(receiving_settings)
    assert context.inbound["inbox"].config.handler == "mail.worker"
    with pytest.raises(ValueError, match="permissions_boundary"):
        _ = ContainerStack(context.container["mail"]).yaml


def test_disabled_alert_keeps_stage_inherited_email(receiving_settings):
    alert = receiving_settings.inbound["inbox"].delivery_alert
    disabled = type(alert).model_validate({"email": alert.email, "enabled": False})
    assert not disabled.enabled


def test_redrive_does_not_delete_on_send_failure(receiving_settings, inlet):
    resource = mock.Mock()
    resource.context = inlet
    resource.stack.output = {
        "DeliveryDLQUrl": "delivery",
        "WorkerDLQUrl": "worker",
        "QueueUrl": "destination",
        "TopicArn": "topic",
        "Bucket": "bucket",
    }
    sqs = mock.Mock()
    envelope = {
        "Type": "Notification",
        "TopicArn": "topic",
        "Message": json.dumps(
            {
                "notificationType": "Received",
                "receipt": {
                    "action": {
                        "type": "S3",
                        "bucketName": "bucket",
                        "objectKey": "raw/message",
                    }
                },
            }
        ),
    }
    sqs.receive_message.return_value = {
        "Messages": [{"Body": json.dumps(envelope), "ReceiptHandle": "handle"}]
    }
    sqs.send_message.side_effect = RuntimeError("配送失敗")
    with (
        mock.patch(
            "pocket_cli.cli.inbound_cli.Settings.from_toml",
            return_value=receiving_settings,
        ),
        mock.patch("pocket_cli.cli.inbound_cli.Inbound", return_value=resource),
        mock.patch("pocket_cli.cli.inbound_cli.boto3.client", return_value=sqs),
    ):
        result = CliRunner().invoke(
            inbound,
            ["--stage", "dev", "--name", "inbox", "redrive", "--queue", "delivery"],
        )
    assert result.exit_code != 0
    sqs.delete_message.assert_not_called()


@mock_aws
def test_failed_conditional_save_reads_winning_metadata(storage):
    s3, config, record, raw = storage
    receiver = Receiver("inbox", config=config, s3=s3)
    first = receiver.receive(record)
    # 最初の存在確認だけが同時保存前だった状況を作る。
    with mock.patch.object(receiver, "_metadata", side_effect=[None, first.metadata]):
        second = receiver.receive(record)
    assert second.raw == raw
    assert second.id == first.id


def test_setup_notification_is_saved_without_business_processing(storage, monkeypatch):
    s3, config, record, raw = storage
    envelope = json.loads(record["body"])
    notification = json.loads(envelope["Message"])
    key = "raw/AMAZON_SES_SETUP_NOTIFICATION"
    notification["receipt"]["action"]["objectKey"] = key
    notification["receipt"]["recipients"] = ["setup@example.com"]
    envelope["Message"] = json.dumps(notification)
    record["body"] = json.dumps(envelope)
    s3.put_object(Bucket=config["bucket"], Key=key, Body=raw)
    monkeypatch.setenv("POCKET_INBOUND", json.dumps({"inbox": config}))
    process = mock.Mock()
    assert handler("inbox", process)({"Records": [record]}, None) == {
        "batchItemFailures": []
    }
    process.assert_not_called()
    assert (
        s3.list_objects_v2(Bucket=config["bucket"], Prefix="metadata/")["KeyCount"] == 1
    )
