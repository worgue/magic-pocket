from __future__ import annotations

from botocore.exceptions import ClientError


def bucket_exists(client, bucket_name: str) -> bool:
    """バケットの存在確認。404以外のエラーはClientErrorとして再送出"""
    try:
        client.head_bucket(Bucket=bucket_name)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "404":
            return False
        raise


def create_bucket(client, bucket_name: str, region: str):
    """リージョンを考慮してバケットを作成"""
    if region == "us-east-1":
        client.create_bucket(Bucket=bucket_name)
    else:
        client.create_bucket(
            Bucket=bucket_name,
            CreateBucketConfiguration={"LocationConstraint": region},
        )
