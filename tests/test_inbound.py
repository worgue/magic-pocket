"""受信口の設定、AWS接続、安全な保存・取り込みの回帰テスト。"""

import json
from unittest import mock

import boto3
import pytest
import yaml
from click.testing import CliRunner
from moto import mock_aws
from pocket_cli.cli.inbound_cli import inbound
from pocket_cli.resources import inbound_domain
from pocket_cli.resources.aws.cloudformation import ContainerStack
from pocket_cli.resources.inbound import Inbound, resolve_rules
from pocket_cli.resources.inbound_domain import (
    InboundDomain,
    cleanup_unused_inbound_domains,
    confirm_inbound_domains,
)
from pocket_cli.resources.inbound_template import (
    build_domain_template,
    build_template,
)
from pydantic import ValidationError

from pocket.context import Context
from pocket.inbound import Receiver, handler, receipt_id
from pocket.inbound_context import InboundContext, InboundDomainContext
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
    with pytest.raises(ValueError, match="create-receipt-rule-set"):
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
        with pytest.raises(ValueError, match="Handler"):
            resource.prepare_deploy()


def _invoke_inbound(receiving_settings, ses, args, **kwargs):
    with (
        mock.patch(
            "pocket_cli.cli.inbound_cli.Settings.from_toml",
            return_value=receiving_settings,
        ),
        mock.patch("pocket_cli.cli.inbound_cli.boto3.client", return_value=ses),
    ):
        return CliRunner().invoke(
            inbound, ["--stage", "dev", "--name", "inbox", *args], **kwargs
        )


def _inbound_mock(name, stack_status="NOEXIST"):
    resource = mock.Mock()
    resource.context.name = name
    resource.context.config.domain = "receive.example.com"
    resource.stack.cfn_status = stack_status
    return resource


def test_cli_destroy_respects_yes(receiving_settings, monkeypatch):
    from pocket_cli.cli import interaction

    monkeypatch.setattr(interaction, "_assume_yes", False)
    resource = _inbound_mock("inbox")
    domain = mock.Mock()
    with (
        mock.patch("pocket_cli.cli.inbound_cli.Inbound", return_value=resource),
        mock.patch("pocket_cli.cli.inbound_cli.InboundDomain", return_value=domain),
    ):
        declined = _invoke_inbound(receiving_settings, mock.Mock(), ["destroy"])
        assert declined.exit_code != 0
        resource.delete.assert_not_called()
        accepted = _invoke_inbound(receiving_settings, mock.Mock(), ["destroy", "-y"])
    assert accepted.exit_code == 0, accepted.output
    resource.delete.assert_called_once_with()
    # このdomainを使う最後の受信口なので、domainのstackも消す
    domain.delete.assert_called_once_with()


def test_cli_destroy_keeps_domain_used_by_other_inbound(receiving_settings):
    settings = Settings.model_validate(_with_second_inlet(receiving_settings))
    target = _inbound_mock("inbox")
    domain = mock.Mock()
    with (
        mock.patch(
            "pocket_cli.cli.inbound_cli.Inbound",
            side_effect=[target, _inbound_mock("billing", "COMPLETED")],
        ),
        mock.patch("pocket_cli.cli.inbound_cli.InboundDomain", return_value=domain),
    ):
        result = _invoke_inbound(settings, mock.Mock(), ["destroy", "-y"])
    assert result.exit_code == 0, result.output
    target.delete.assert_called_once_with()
    domain.delete.assert_not_called()
    assert "inbound.billing" in result.output


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


def _with_second_inlet(receiving_settings, **overrides):
    """同じdomainを別handlerで受ける2つ目のinboundを足した設定を返す。"""
    data = receiving_settings.model_dump(exclude_computed_fields=True)
    data["container"]["mail"]["handlers"]["billing"] = dict(
        data["container"]["mail"]["handlers"]["worker"], command="app.mail.billing"
    )
    data["inbound"]["billing"] = dict(
        data["inbound"]["inbox"],
        recipients=["billing@receive.example.com"],
        handler="mail.billing",
        **overrides,
    )
    return data


def test_inbounds_on_same_domain_share_one_domain_stack(receiving_settings):
    settings = Settings.model_validate(_with_second_inlet(receiving_settings))
    domains = InboundDomainContext.from_settings(settings)
    assert list(domains) == ["receive.example.com"]
    domain = domains["receive.example.com"]
    assert domain.stack_name.startswith("dev-receiving-")
    assert len(domain.stack_name) <= 128
    # stage が違えば stack も別 (sandbox と dev で共有しない)
    settings.stage = "sandbox"
    settings.general.stages = ["sandbox"]
    other = InboundDomainContext.from_settings(settings)["receive.example.com"]
    assert other.stack_name != domain.stack_name


def test_same_domain_requires_consistent_dns_settings(receiving_settings):
    with pytest.raises(ValidationError, match="揃えて"):
        Settings.model_validate(
            _with_second_inlet(receiving_settings, manage_dns=False)
        )
    data = receiving_settings.model_dump(exclude_computed_fields=True)
    data["inbound"]["inbox"].update(manage_dns=False, hosted_zone_id_override="Z1")
    with pytest.raises(ValidationError, match="hosted_zone_id_override"):
        Settings.model_validate(data)


def test_domain_template_owns_identity_dkim_and_mx(receiving_settings):
    domain = InboundDomainContext.from_settings(receiving_settings)[
        "receive.example.com"
    ]
    template = build_domain_template(domain, "ZONE123")
    resources = template["Resources"]
    assert resources["Identity"]["Type"] == "AWS::SES::EmailIdentity"
    assert resources["Identity"]["Properties"]["EmailIdentity"] == domain.domain
    for i in (1, 2, 3):
        record = resources[f"DkimRecord{i}"]["Properties"]
        assert record["Type"] == "CNAME"
        assert record["HostedZoneId"] == "ZONE123"
        assert record["Name"] == {"Fn::GetAtt": ["Identity", f"DkimDNSTokenName{i}"]}
        assert record["ResourceRecords"] == [
            {"Fn::GetAtt": ["Identity", f"DkimDNSTokenValue{i}"]}
        ]
    mx = resources["MxRecord"]["Properties"]
    assert (mx["Name"], mx["Type"]) == ("receive.example.com", "MX")
    assert mx["ResourceRecords"] == ["10 inbound-smtp.ap-northeast-1.amazonaws.com"]
    # 外部DNS: identityだけを作り、登録すべき値はOutputsに残す
    external = build_domain_template(domain, None)
    assert list(external["Resources"]) == ["Identity"]
    assert "DkimValue3" in external["Outputs"]
    assert external["Outputs"]["MxValue"]["Value"] == domain.mx_value


class _FakeDomain(InboundDomain):
    """stackの状態とSESの検証状態を差し替えたInboundDomain。"""

    def __init__(self, settings, *, stack_status, statuses):
        super().__init__(
            InboundDomainContext.from_settings(settings)["receive.example.com"]
        )
        self.fake_stack = mock.Mock()
        self.fake_stack.cfn_status = stack_status
        self.statuses = list(statuses)
        self.calls = 0

    @property
    def stack(self):
        return self.fake_stack

    def verification_status(self):
        self.calls += 1
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]


def test_existing_identity_is_never_adopted(receiving_settings):
    foreign = _FakeDomain(
        receiving_settings, stack_status="NOEXIST", statuses=["SUCCESS"]
    )
    with pytest.raises(ValueError, match="delete-email-identity"):
        foreign.prepare_deploy()
    # 自分のstackが持つidentityなら通す / identityが無ければ新規作成へ進む
    _FakeDomain(
        receiving_settings, stack_status="COMPLETED", statuses=["SUCCESS"]
    ).prepare_deploy()
    _FakeDomain(
        receiving_settings, stack_status="NOEXIST", statuses=[None]
    ).prepare_deploy()


def test_deploy_waits_for_verification_and_times_out(receiving_settings, monkeypatch):
    monkeypatch.setattr(inbound_domain.time, "sleep", lambda _: None)
    resource = _FakeDomain(
        receiving_settings,
        stack_status="COMPLETED",
        statuses=["PENDING", "PENDING", "SUCCESS"],
    )
    resource.wait_verified()
    assert resource.calls == 3
    monkeypatch.setattr(inbound_domain, "VERIFY_TIMEOUT", 0)
    pending = _FakeDomain(
        receiving_settings, stack_status="COMPLETED", statuses=["PENDING"]
    )
    with pytest.raises(ValueError, match="検証が完了しません"):
        pending.wait_verified()


def test_external_dns_is_reported_without_waiting(receiving_settings, capsys):
    receiving_settings.inbound["inbox"].manage_dns = False
    context = mock.Mock()
    context.inbound_domain = InboundDomainContext.from_settings(receiving_settings)
    with (
        mock.patch.object(InboundDomain, "verification_status", return_value="PENDING"),
        mock.patch.object(InboundDomain, "wait_verified") as wait,
        mock.patch.object(
            InboundDomain,
            "dns_records",
            return_value=[("MX", "receive.example.com", "10 inbound-smtp")],
        ),
    ):
        confirm_inbound_domains(context)
    wait.assert_not_called()
    assert "MX receive.example.com 10 inbound-smtp" in capsys.readouterr().out


def test_unused_domain_stack_is_deleted_after_domain_change(receiving_settings):
    context = mock.Mock()
    context.general.region = "ap-northeast-1"
    context.inbound_domain = InboundDomainContext.from_settings(receiving_settings)
    state_store = mock.Mock()
    state_store.load.return_value = {
        "resources": {
            "inbound_domain": {
                "receive.example.com": {"stack_name": "current"},
                "old.example.com": {"stack_name": "old-domain-stack"},
            }
        }
    }
    client = mock.Mock()
    with mock.patch.object(inbound_domain.boto3, "client", return_value=client):
        cleanup_unused_inbound_domains(context, state_store)
    client.delete_stack.assert_called_once_with(StackName="old-domain-stack")
