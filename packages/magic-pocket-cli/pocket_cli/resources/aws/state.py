from __future__ import annotations

import json

import boto3
from botocore.exceptions import ClientError

from pocket.settings import _deep_merge
from pocket_cli.resources.aws.s3_utils import (
    bucket_exists,
    bucket_region,
    create_bucket,
    delete_bucket_with_contents,
)

STATE_BUCKET_REGION_DOCS = (
    "https://worgue.github.io/magic-pocket/guide/configuration/#state-bucket-region"
)


class StateBucketRegionError(RuntimeError):
    """state bucket が [general] region と別の region にある"""

    def __init__(self, bucket_name: str, actual: str, expected: str):
        super().__init__(
            "state bucket %s は %s にありますが、[general] region は %s です。"
            "region を移したときに state bucket だけ旧 region に残っています。"
            "state bucket を %s へ移してから再実行してください (手順: %s)。"
            % (bucket_name, actual, expected, expected, STATE_BUCKET_REGION_DOCS)
        )


class StateStore:
    STATE_KEY = "resources.json"

    def __init__(self, bucket_name: str, region: str):
        self.bucket_name = bucket_name
        self.region = region
        self.client = boto3.client("s3", region_name=region)
        self._state: dict | None = None

    def ensure_bucket(self):
        """state バケットが存在しなければ作成（全公開ブロック）。

        既存の bucket が region 以外にあればエラーにする (StateBucketRegionError)。
        boto3 は HeadBucket の 301 を自動で追うため、region 移設で bucket だけ旧 region
        に残ったずれは黙って吸収される。吸収すると気付けず、CodeBuild (source bucket
        が project と同じ region であることを求める) で分かりにくく落ちるため止める
        """
        if bucket_exists(self.client, self.bucket_name):
            actual = bucket_region(self.client, self.bucket_name)
            if actual != self.region:
                raise StateBucketRegionError(self.bucket_name, actual, self.region)
            return
        create_bucket(self.client, self.bucket_name, self.region)
        self.client.put_public_access_block(
            Bucket=self.bucket_name,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )

    def load(self) -> dict:
        """S3から resources.json を読み込み。存在しなければ空stateを返す"""
        if self._state is not None:
            return self._state
        try:
            response = self.client.get_object(
                Bucket=self.bucket_name, Key=self.STATE_KEY
            )
            self._state = json.loads(response["Body"].read().decode("utf-8"))
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "NoSuchKey":
                self._state = {"version": 1, "resources": {}}
            else:
                raise
        return self._state  # type: ignore[return-value]

    def save(self):
        """現在のstateをS3に書き込み"""
        state = self.load()
        self.client.put_object(
            Bucket=self.bucket_name,
            Key=self.STATE_KEY,
            Body=json.dumps(state, indent=2).encode("utf-8"),
            ContentType="application/json",
        )

    def record(self, info: dict):
        """リソース情報を deep merge して保存"""
        state = self.load()
        _deep_merge(state["resources"], info)
        self.save()

    def delete_bucket(self):
        """ステートバケットを中身ごと削除"""
        delete_bucket_with_contents(self.client, self.bucket_name)


def context_resource_prefix(context) -> str:
    """Context から resource prefix を算出する (deploy / destroy / build 共通)。"""
    if not context.general:
        raise RuntimeError("general context is not configured")
    return context.general.prefix_template.format(
        stage=context.stage,
        project=context.project_name,
        namespace=context.general.namespace,
    )


def create_state_store(context) -> StateStore:
    """Context から StateStore を作成する (deploy / destroy 共通)。"""
    resource_prefix = context_resource_prefix(context)
    return StateStore(f"{resource_prefix}state", context.general.region)
