from __future__ import annotations

import json

import boto3
import mergedeep
from botocore.exceptions import ClientError

from .s3_utils import bucket_exists, create_bucket, delete_bucket_with_contents


class StateStore:
    STATE_KEY = "resources.json"

    def __init__(self, bucket_name: str, region: str):
        self.bucket_name = bucket_name
        self.region = region
        self.client = boto3.client("s3", region_name=region)
        self._state: dict | None = None

    def ensure_bucket(self):
        """state バケットが存在しなければ作成（全公開ブロック）"""
        if bucket_exists(self.client, self.bucket_name):
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
        """リソース情報をmerge（mergedeep使用）して保存"""
        state = self.load()
        mergedeep.merge(state["resources"], info)
        self.save()

    def delete_bucket(self):
        """ステートバケットを中身ごと削除"""
        delete_bucket_with_contents(self.client, self.bucket_name)
