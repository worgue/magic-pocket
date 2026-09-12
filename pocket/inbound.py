"""SES原本と受信情報を保全してから利用側の処理へ渡す。Djangoには依存しない。"""

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from typing import Callable

import boto3
from botocore.exceptions import BotoCoreError, ClientError

SETUP_NOTIFICATION = "AMAZON_SES_SETUP_NOTIFICATION"


def receipt_id(bucket: str, key: str) -> str:
    """SNS再送でも変わらない、原本オブジェクト単位の識別子。"""
    return hashlib.sha256(f"{bucket}\n{key}".encode()).hexdigest()


@dataclass(frozen=True)
class ReceivedMail:
    id: str
    raw: bytes
    metadata: dict

    @property
    def is_setup(self) -> bool:
        return self.metadata["source"]["key"].endswith("/" + SETUP_NOTIFICATION)

    def parse(self) -> EmailMessage:
        """保存済みの原本を標準ライブラリで解析する。"""
        message = BytesParser(policy=policy.default).parsebytes(self.raw)
        if not isinstance(message, EmailMessage):
            raise TypeError("EmailMessage を生成できませんでした")
        return message


class Receiver:
    """POCKET_INBOUNDから受信口を選び、1件ずつ保存する。"""

    def __init__(self, name: str, *, config: dict | None = None, s3=None):
        self.config = (
            config
            if config is not None
            else json.loads(os.environ["POCKET_INBOUND"])[name]
        )
        self.s3 = (
            s3
            if s3 is not None
            else boto3.client("s3", region_name=self.config["region"])
        )

    def receive(self, record: dict) -> ReceivedMail:
        """SNS envelopeを検証し、原本の版・hashと完全な通知を条件付き保存する。"""
        envelope = json.loads(record["body"])
        if (
            envelope.get("Type") != "Notification"
            or envelope.get("TopicArn") != self.config["topic_arn"]
        ):
            raise ValueError("想定外のSNS通知です")
        notification = json.loads(envelope["Message"])
        if notification.get("notificationType") != "Received":
            raise ValueError("SES受信通知ではありません")
        receipt = notification["receipt"]
        action = receipt["action"]
        bucket, key = action["bucketName"], action["objectKey"]
        if (
            action.get("type") != "S3"
            or bucket != self.config["bucket"]
            or not key.startswith(self.config["raw_prefix"])
            or action.get("topicArn") != self.config["topic_arn"]
        ):
            raise ValueError("想定外のSES保存先です")
        recipients = receipt["recipients"]
        if key != self.config["raw_prefix"] + SETUP_NOTIFICATION and (
            not recipients
            or not set(recipients).intersection(self.config["recipients"])
        ):
            raise ValueError("受信宛先が設定と一致しません")
        identifier = receipt_id(bucket, key)
        metadata_key = self.config["metadata_prefix"] + identifier + ".json"
        existing = self._metadata(metadata_key)
        if existing is not None:
            return self._load(existing)
        response = self.s3.get_object(Bucket=bucket, Key=key)
        raw = response["Body"].read()
        version = response.get("VersionId")
        if not version or version == "null":
            raise ValueError("原本バケットのversioningが有効ではありません")
        metadata = {
            "schema_version": 1,
            "receipt_id": identifier,
            "source": {
                "bucket": bucket,
                "key": key,
                "version_id": version,
                "sha256": hashlib.sha256(raw).hexdigest(),
            },
            "sns": envelope,
            "ses": notification,
        }
        try:
            self.s3.put_object(
                Bucket=bucket,
                Key=metadata_key,
                Body=json.dumps(metadata, ensure_ascii=False).encode(),
                ContentType="application/json",
                IfNoneMatch="*",
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "PreconditionFailed":
                raise
            # 同時受信時は先に保存された原本の版とメタデータに揃える。
            saved = self._metadata(metadata_key)
            if saved is None:
                raise RuntimeError("同時保存された受信情報を取得できません") from error
            return self._load(saved)
        return ReceivedMail(identifier, raw, metadata)

    def _metadata(self, key: str) -> dict | None:
        try:
            response = self.s3.get_object(Bucket=self.config["bucket"], Key=key)
        except ClientError as error:
            if error.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise
        return json.loads(response["Body"].read())

    def _load(self, metadata: dict) -> ReceivedMail:
        source = metadata["source"]
        if source["bucket"] != self.config["bucket"] or not source["key"].startswith(
            self.config["raw_prefix"]
        ):
            raise ValueError("保存済みメタデータの原本参照が不正です")
        raw = self.s3.get_object(
            Bucket=source["bucket"], Key=source["key"], VersionId=source["version_id"]
        )["Body"].read()
        if hashlib.sha256(raw).hexdigest() != source["sha256"]:
            raise ValueError("原本のhashが一致しません")
        return ReceivedMail(metadata["receipt_id"], raw, metadata)

    def load_import(self, manifest_key: str) -> ReceivedMail:
        """明示した取り込みジョブ専用。S3へのコピーだけでは起動しない。"""
        if not manifest_key.startswith(
            self.config["import_prefix"]
        ) or not manifest_key.endswith("/manifest.json"):
            raise ValueError("imports内のmanifest.jsonを指定してください")
        manifest = self._metadata(manifest_key)
        if manifest is None:
            raise ValueError("取り込みmanifestがありません")
        prefix = manifest_key.removesuffix("manifest.json")
        raw = self.s3.get_object(Bucket=self.config["bucket"], Key=prefix + "raw.eml")[
            "Body"
        ].read()
        metadata = self._metadata(prefix + "metadata.json")
        if (
            metadata is None
            or metadata["source"]["sha256"] != hashlib.sha256(raw).hexdigest()
        ):
            raise ValueError("取り込み原本と受信情報が一致しません")
        if manifest["receipt_id"] != metadata["receipt_id"]:
            raise ValueError("取り込みmanifestの受信IDが一致しません")
        return ReceivedMail(metadata["receipt_id"], raw, metadata)


def handler(name: str, process: Callable[[ReceivedMail], None]):
    """受信情報を保存後にprocessを実行するLambda entrypointを作る。

    processの副作用はmail.idをキーに冪等化する。未分類の利用側例外は伝播し、
    バッチ全体を再試行する。例外内容やメール本文はこの層ではログに出さない。
    """

    def receive_batch(event, context):
        receiver = Receiver(name)
        failures = []
        for record in event["Records"]:
            try:
                mail = receiver.receive(record)
                if not mail.is_setup:
                    process(mail)
            except (
                ValueError,
                KeyError,
                TypeError,
                BotoCoreError,
                ClientError,
            ) as error:
                logging.getLogger(__name__).error(
                    "受信処理失敗: SQS ID=%s / %s",
                    record["messageId"],
                    type(error).__name__,
                )
                failures.append({"itemIdentifier": record["messageId"]})
        return {"batchItemFailures": failures}

    return receive_batch


def validate_receipt_id(value: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("受信IDは64桁のSHA256です")
