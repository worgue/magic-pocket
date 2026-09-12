"""受信ルールの共有境界と、専用スタックのライフサイクル。"""

import json
from datetime import datetime, timedelta, timezone
from functools import cached_property

import boto3
import yaml
from botocore.exceptions import ClientError

from pocket.inbound_context import InboundContext
from pocket_cli.resources.aws.cloudformation import Stack, _is_stack_not_exist_error
from pocket_cli.resources.aws.stack_backed import StackBackedResource
from pocket_cli.resources.inbound_template import build_template


def resolve_rules(
    active: dict, ctx: InboundContext, deployed: dict | None
) -> tuple[str, str | None]:
    """明示指定なしならactive setへ末尾追加。既存bindingの漂流は拒否する。"""
    name = active.get("Metadata", {}).get("Name")
    if not name:
        raise ValueError(
            "active receipt rule set がありません。"
            "pocket resource inbound init を実行してください"
        )
    if ctx.config.rule_set and ctx.config.rule_set != name:
        raise ValueError("明示したrule_setがactive setと一致しません")
    expected = (deployed or {}).get("RuleSet") or ctx.config.rule_set
    if expected and expected != name:
        raise ValueError(f"active rule set が {expected} から {name} に変わっています")
    rules = active.get("Rules", [])
    own = next((r for r in rules if r["Name"] == ctx.resource_name), None)
    if deployed and not own:
        raise ValueError(
            "管理中の受信ルールが見つかりません。削除・移動を確認してください"
        )
    if own and not deployed:
        raise ValueError("同名の未管理ルールがあります。既存ルールを取り込みません")
    for rule in rules:
        if rule is own or not rule.get("Enabled", False):
            continue
        recipients = rule.get("Recipients", [])
        # 複数宛先メールでは、別宛先に一致した停止処理も配送全体に影響する。
        stopping = any(
            "StopAction" in a or "BounceAction" in a or "LambdaAction" in a
            for a in rule.get("Actions", [])
        )
        overlap = not recipients or any(
            matches_address(pattern, address)
            for pattern in recipients
            for address in ctx.config.recipients
        )
        if stopping or overlap:
            raise ValueError(
                f"既存ルール {rule['Name']} と競合します"
                "（宛先重複または停止・Lambda処理）"
            )
    if deployed:
        index = rules.index(own)
        return name, rules[index - 1]["Name"] if index else None
    return name, rules[-1]["Name"] if rules else None


def matches_address(pattern: str, address: str) -> bool:
    pattern = pattern.lower()
    address = address.lower()
    domain = address.split("@", 1)[1]
    if "@" in pattern:
        return pattern == address
    if pattern.startswith("."):
        return domain.endswith(pattern)
    return domain == pattern


class InboundStack(Stack):
    context: InboundContext
    template_filename = "inbound"

    @property
    def name(self):
        return self.context.resource_name

    @property
    def export(self):
        return {}

    @cached_property
    def resolved_binding(self):
        active = boto3.client(
            "ses", region_name=self.context.region
        ).describe_active_receipt_rule_set()
        return resolve_rules(active, self.context, self.output)

    def binding(self):
        return self.resolved_binding

    @property
    def yaml(self):
        rule_set, after = self.binding()
        return json.dumps(build_template(self.context, rule_set, after), indent=2)


class Inbound(StackBackedResource):
    context: InboundContext

    @cached_property
    def _stack(self) -> InboundStack:
        return InboundStack(self.context)

    @property
    def stack(self) -> InboundStack:
        return self._stack

    def prepare_deploy(self, mediator=None):
        self.stack.binding()
        ses = boto3.client("ses", region_name=self.context.region)
        domain = self.context.config.domain
        result = ses.get_identity_verification_attributes(Identities=[domain])
        if (
            result.get("VerificationAttributes", {})
            .get(domain, {})
            .get("VerificationStatus")
            != "Success"
        ):
            raise ValueError(
                f"{domain} のSES検証が未完了です。"
                "inbound init のDNSレコードを登録してください"
            )
        if self.stack.output:
            for key, value in (
                ("Handler", self.context.config.handler),
                ("RawPrefix", self.context.config.raw_prefix),
                ("MetadataPrefix", self.context.config.metadata_prefix),
                ("ImportPrefix", self.context.config.import_prefix),
            ):
                if self.stack.output.get(key) != value:
                    raise ValueError(f"{key}の変更は新しいinbound名で移行してください")
            previous = self.stack.uploaded_template
            old = yaml.safe_load(previous) if isinstance(previous, str) else previous
            if old:
                old_rule = old["Resources"]["ReceiptRule"]["Properties"]["Rule"]
                if (
                    old_rule["Enabled"]
                    and old_rule["Recipients"] != self.context.config.recipients
                ):
                    raise ValueError(
                        "宛先変更前に enabled = false でdeployし、"
                        "滞留を処理してください"
                    )
                old_bucket = old["Resources"]["Bucket"]["Properties"]
                current = build_template(self.context, *self.stack.binding())[
                    "Resources"
                ]["Bucket"]["Properties"]
                if old_bucket["BucketName"] != current["BucketName"]:
                    raise ValueError(
                        "受信バケットの置換はできません。新しい受信口を作成してください"
                    )

    def ensure_post_deploy_state(self):
        self.stack.__dict__.pop("resolved_binding", None)
        self.stack.binding()
        output = self.stack.output
        if not output:
            raise ValueError("受信スタックの出力を確認できません")
        ses = boto3.client("ses", region_name=self.context.region)
        rule = ses.describe_receipt_rule(
            RuleSetName=output["RuleSet"], RuleName=output["RuleName"]
        )["Rule"]
        expected = {
            "Name": self.context.resource_name,
            "Enabled": self.context.config.enabled,
            "TlsPolicy": self.context.config.tls_policy,
            "ScanEnabled": self.context.config.scan_enabled,
            "Recipients": self.context.config.recipients,
            "Actions": [
                {
                    "S3Action": {
                        "BucketName": output["Bucket"],
                        "ObjectKeyPrefix": self.context.config.raw_prefix,
                        "TopicArn": output["TopicArn"],
                    }
                }
            ],
        }
        if rule != expected:
            raise ValueError("SES受信ルールがdeployした宣言と一致しません")

    def state_info(self):
        return {"inbound": {self.context.name: {"stack_name": self.stack.name}}}

    def delete(self):
        if self.stack.status_detail == "DELETE_IN_PROGRESS":
            self.stack.wait_status("NOEXIST", timeout=300, interval=10)
            return
        if self.stack.cfn_status == "NOEXIST":
            return
        output = self.stack.output
        if not output:
            raise ValueError(
                "受信スタックの出力がありません。AWS上の状態を確認してください"
            )
        # 稼働中に削除を開始しない。falseでdeployした後に配送猶予と滞留を確認する。
        ses = boto3.client("ses", region_name=self.context.region)
        rule = ses.describe_receipt_rule(
            RuleSetName=output["RuleSet"], RuleName=output["RuleName"]
        )["Rule"]
        if rule["Enabled"] or output.get("Enabled") != "false":
            raise ValueError(
                "先に inbound.enabled = false でdeployし、"
                "36時間以上の配送猶予を置いてください"
            )
        description = self.stack.description
        if not description:
            raise ValueError("受信停止時刻を確認できません")
        changed = description.get("LastUpdatedTime", description["CreationTime"])
        if datetime.now(timezone.utc) - changed < timedelta(hours=36):
            raise ValueError("最後のスタック更新から36時間の配送猶予が必要です")
        sqs = boto3.client("sqs", region_name=self.context.region)
        for key in ("QueueUrl", "WorkerDLQUrl", "DeliveryDLQUrl"):
            attrs = sqs.get_queue_attributes(
                QueueUrl=output[key],
                AttributeNames=[
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible",
                    "ApproximateNumberOfMessagesDelayed",
                ],
            )["Attributes"]
            if any(int(value) for value in attrs.values()):
                raise ValueError(
                    f"{key} に滞留があります。処理または退避してから再実行してください"
                )
        self._delete_stack()


def check_removed_inbound(context, state_store):
    """宣言だけ消して受信ルールやqueueを孤立させるdeployを拒否する。"""
    saved = state_store.load().get("resources", {}).get("inbound", {})
    if not saved:
        return
    client = boto3.client("cloudformation", region_name=context.general.region)
    for name, info in saved.items():
        if name in context.inbound:
            continue
        try:
            response = client.describe_stacks(StackName=info["stack_name"])
        except ClientError as error:
            if _is_stack_not_exist_error(error):
                continue
            raise
        if response["Stacks"][0]["StackStatus"] != "DELETE_COMPLETE":
            raise ValueError(
                f"inbound.{name}がAWS上に残っています。宣言を戻して受信を停止し、"
                "inbound destroyを完了してから宣言を削除してください"
            )


def echo_inbound_details(context):
    """deploy/statusで接続先とalarm ARNを示し、外部通知の未検証を区別する。"""
    for name, inlet in context.inbound.items():
        output = Inbound(inlet).stack.output
        if not output:
            continue
        print(
            f"inbound.{name}: rule set={output['RuleSet']} / bucket={output['Bucket']}"
        )
        for key, value in output.items():
            if key.endswith("AlarmArn"):
                print(f"  {key}: {value}")
        if (
            inlet.config.delivery_alert.enabled
            and inlet.config.delivery_alert.eventbridge
        ):
            print(
                "  alarm作成済み。EventBridgeから外部通知先への接続・着信は未検証です。"
            )
