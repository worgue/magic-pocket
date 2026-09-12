"""受信口の初期設定と原本・受信情報の運用コマンド。"""

import hashlib
import json

import boto3
import click
from botocore.config import Config

from pocket.inbound import SETUP_NOTIFICATION, receipt_id, validate_receipt_id
from pocket.inbound_context import InboundContext
from pocket.settings import Settings, parse_handler_ref
from pocket_cli.resources.inbound import Inbound


@click.group()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", required=True)
@click.option("--name", required=True, help="inboundの設定名")
@click.pass_context
def inbound(ctx, stage, name):
    """SES受信口を管理する。"""
    settings = Settings.from_toml(stage=stage)
    if name not in settings.inbound:
        raise click.ClickException(f"inbound.{name} がありません")
    ctx.obj = Inbound(InboundContext.from_settings(name, settings))
    ctx.meta["settings"] = settings


@inbound.command()
@click.pass_obj
def init(resource):
    """共有rule setの初期化とドメイン検証の申請（初回のみ）。"""
    ses = boto3.client("ses", region_name=resource.context.region)
    active = ses.describe_active_receipt_rule_set().get("Metadata", {}).get("Name")
    if not active:
        name = "pocket-inbound"
        names = [
            item["Name"]
            for page in ses.get_paginator("list_receipt_rule_sets").paginate()
            for item in page["RuleSets"]
        ]
        if name in names:
            raise click.ClickException(
                "pocket-inboundが既にあります。内容を確認し管理者が有効化してください"
            )
        click.confirm(
            "このaccount/regionで初期化を同時実行していないことを確認してください。"
            "共有rule set pocket-inboundを作成・有効化しますか？",
            abort=True,
        )
        if ses.describe_active_receipt_rule_set().get("Metadata"):
            raise click.ClickException("active setが変わりました。再実行してください")
        ses.create_receipt_rule_set(RuleSetName=name)
        if ses.describe_active_receipt_rule_set().get("Metadata"):
            raise click.ClickException(
                "初期化中にactive setが変わりました。有効化を中止します"
            )
        ses.set_active_receipt_rule_set(RuleSetName=name)
        active = ses.describe_active_receipt_rule_set().get("Metadata", {}).get("Name")
        if active != name:
            raise click.ClickException("有効化後のactive setが一致しません")
    domain = resource.context.config.domain
    result = ses.get_identity_verification_attributes(Identities=[domain])
    identity = result.get("VerificationAttributes", {}).get(domain, {})
    token = identity.get("VerificationToken")
    if not token and identity.get("VerificationStatus") != "Success":
        token = ses.verify_domain_identity(Domain=domain)["VerificationToken"]
    click.echo(f"active rule set: {active}")
    if token:
        click.echo(f"TXT _amazonses.{domain} {token}")
    click.echo(f"MX {domain} 10 inbound-smtp.{resource.context.region}.amazonaws.com")
    click.echo("SES受信対応リージョンとMXの既存配送先を確認してDNSを登録してください。")


@inbound.command()
@click.pass_obj
def status(resource):
    """スタック・active set・検証状態・監視先を表示する。"""
    resource.prepare_deploy()
    click.echo(f"stack: {resource.stack.name} / {resource.status}")
    click.echo(json.dumps(resource.stack.output or {}, ensure_ascii=False, indent=2))
    if resource.context.config.delivery_alert.eventbridge:
        click.echo(
            "EventBridge: source=aws.cloudwatch, "
            "detail-type=CloudWatch Alarm State Change"
        )


@inbound.command(name="yaml")
@click.pass_obj
def show_yaml(resource):
    """active setと既存bindingを照会しテンプレートを表示する。"""
    click.echo(resource.stack.yaml)


@inbound.command()
@click.pass_obj
def destroy(resource):
    """無効化・配送猶予・滞留解消後に自分のルールとスタックを削除する。"""
    click.confirm(
        "受信停止後36時間以上経過し、原本の照合・必要な退避を終えましたか？", abort=True
    )
    resource.delete()


@inbound.command()
@click.pass_obj
def reconcile(resource):
    """原本に対応する受信情報の欠落を検出する（復元・推測はしない）。"""
    output = resource.stack.output
    if not output:
        raise click.ClickException("受信スタックがありません")
    bucket = output["Bucket"]
    s3 = boto3.client("s3", region_name=resource.context.region)
    config = resource.context.config
    metadata = {
        item["Key"]
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=config.metadata_prefix
        )
        for item in page.get("Contents", [])
    }
    missing = 0
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=config.raw_prefix
    ):
        for item in page.get("Contents", []):
            if item["Key"] == config.raw_prefix + SETUP_NOTIFICATION:
                continue
            identifier = receipt_id(bucket, item["Key"])
            if config.metadata_prefix + identifier + ".json" not in metadata:
                click.echo(f"受信情報なし: {item['Key']} / {identifier}")
                missing += 1
    if missing:
        raise click.ClickException(
            f"{missing}件の受信情報がありません。通知の遅延・DLQを確認してください"
        )
    click.echo("原本に対応する受信情報を確認しました。")


@inbound.command(name="copy")
@click.option("--receipt-id", "identifier", required=True)
@click.option("--to-stage", required=True)
@click.option("--to-name", required=True)
@click.pass_obj
def copy_receipt(resource, identifier, to_stage, to_name):
    """指定した1件の原本と受信情報を別stageへコピーしmanifestを最後に保存する。"""
    validate_receipt_id(identifier)
    settings = Settings.from_toml(stage=to_stage)
    if to_name not in settings.inbound:
        raise click.ClickException(f"コピー先のinbound.{to_name}がありません")
    target = Inbound(InboundContext.from_settings(to_name, settings))
    source_output, target_output = resource.stack.output, target.stack.output
    if not source_output or not target_output:
        raise click.ClickException("コピー元と先の受信スタックが必要です")
    source = boto3.client("s3", region_name=resource.context.region)
    destination = boto3.client("s3", region_name=target.context.region)
    key = resource.context.config.metadata_prefix + identifier + ".json"
    metadata_bytes = source.get_object(Bucket=source_output["Bucket"], Key=key)[
        "Body"
    ].read()
    metadata = json.loads(metadata_bytes)
    reference = metadata["source"]
    if reference["bucket"] != source_output["Bucket"] or not reference[
        "key"
    ].startswith(resource.context.config.raw_prefix):
        raise click.ClickException("原本の参照先が設定と一致しません")
    raw = source.get_object(
        Bucket=reference["bucket"],
        Key=reference["key"],
        VersionId=reference["version_id"],
    )["Body"].read()
    if (
        metadata["receipt_id"] != identifier
        or hashlib.sha256(raw).hexdigest() != reference["sha256"]
    ):
        raise click.ClickException("原本のhashまたは受信IDが一致しません")
    # 同じ受信IDのcopyは同じ内容になる。manifestが揃うまで解析ジョブを投入しない。
    prefix = target.context.config.import_prefix + identifier + "/"
    for suffix, body, content_type in (
        ("raw.eml", raw, "message/rfc822"),
        ("metadata.json", metadata_bytes, "application/json"),
        (
            "manifest.json",
            json.dumps({"schema_version": 1, "receipt_id": identifier}).encode(),
            "application/json",
        ),
    ):
        destination.put_object(
            Bucket=target_output["Bucket"],
            Key=prefix + suffix,
            Body=body,
            ContentType=content_type,
        )
    click.echo(prefix + "manifest.json")
    click.echo(
        "コピー完了。取り込み用handlerからReceiver.load_importで明示的に解析してください。"
    )


@inbound.command(name="import")
@click.option("--manifest-key", required=True)
@click.option("--handler", "handler_ref", required=True, help="取り込み用handlerの参照")
@click.pass_context
def import_receipt(ctx, manifest_key, handler_ref):
    """コピー済みmanifestを指定し、利用側の取り込みhandlerを同期実行する。"""
    resource = ctx.obj
    settings = ctx.meta["settings"]
    container, key, _ = settings.resolve_handler(handler_ref)
    receiving_container, _ = parse_handler_ref(resource.context.config.handler)
    if (
        container != receiving_container
        or handler_ref == resource.context.config.handler
    ):
        raise click.ClickException(
            "受信workerと同じcontainerの別handlerを指定してください"
        )
    if not manifest_key.startswith(
        resource.context.config.import_prefix
    ) or not manifest_key.endswith("/manifest.json"):
        raise click.ClickException("imports内のmanifest.jsonを指定してください")
    prefix = settings.prefix_template.format(**settings.format_vars)
    client = boto3.client(
        "lambda",
        region_name=resource.context.region,
        config=Config(read_timeout=900, retries={"total_max_attempts": 1}),
    )
    response = client.invoke(
        FunctionName=f"{prefix}{container}-{key}",
        InvocationType="RequestResponse",
        Payload=json.dumps(
            {"inbound": resource.context.name, "manifest_key": manifest_key}
        ).encode(),
    )
    response["Payload"].read()
    if response.get("FunctionError"):
        raise click.ClickException(
            "取り込みhandlerが失敗しました。Lambdaログを確認してください"
        )
    click.echo("取り込みhandlerの実行が完了しました。")


@inbound.command()
@click.option("--queue", type=click.Choice(["delivery", "worker"]), required=True)
@click.option("--limit", type=click.IntRange(1, 100), default=10, show_default=True)
@click.pass_obj
def redrive(resource, queue, limit):
    """DLQの保存形式を検証し、受信queueへ戻す。成功した送信だけ削除する。"""
    output = resource.stack.output
    if not output:
        raise click.ClickException("受信スタックがありません")
    sqs = boto3.client("sqs", region_name=resource.context.region)
    source = output["DeliveryDLQUrl" if queue == "delivery" else "WorkerDLQUrl"]
    count = 0
    for _ in range(limit):
        messages = sqs.receive_message(
            QueueUrl=source, MaxNumberOfMessages=1, VisibilityTimeout=60
        ).get("Messages", [])
        if not messages:
            break
        message = messages[0]
        envelope = json.loads(message["Body"])
        if (
            envelope.get("Type") != "Notification"
            or envelope.get("TopicArn") != output["TopicArn"]
        ):
            raise click.ClickException(
                "想定外の通知形式です。DLQから削除せず中止します"
            )
        notification = json.loads(envelope["Message"])
        action = notification.get("receipt", {}).get("action", {})
        if (
            notification.get("notificationType") != "Received"
            or action.get("type") != "S3"
            or action.get("bucketName") != output["Bucket"]
            or not action.get("objectKey", "").startswith(
                resource.context.config.raw_prefix
            )
        ):
            raise click.ClickException(
                "想定外のSES保存先です。DLQから削除せず中止します"
            )
        sqs.send_message(QueueUrl=output["QueueUrl"], MessageBody=message["Body"])
        sqs.delete_message(QueueUrl=source, ReceiptHandle=message["ReceiptHandle"])
        count += 1
    click.echo(
        f"{count}件を受信queueへ再投入しました。重複実行に備え業務側で冪等化してください。"
    )
