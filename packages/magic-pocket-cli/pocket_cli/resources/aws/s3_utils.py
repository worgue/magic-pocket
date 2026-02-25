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


def empty_bucket(client, bucket_name: str):
    """バケット内の全オブジェクト（バージョン含む）を削除"""
    # 通常オブジェクトの削除
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket_name):
        objects = page.get("Contents", [])
        if not objects:
            continue
        delete_keys = [{"Key": obj["Key"]} for obj in objects]
        client.delete_objects(Bucket=bucket_name, Delete={"Objects": delete_keys})

    # バージョニングが有効な場合のバージョン・DeleteMarker削除
    paginator = client.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket_name):
        delete_keys = []
        for version in page.get("Versions", []):
            delete_keys.append(
                {"Key": version["Key"], "VersionId": version["VersionId"]}
            )
        for marker in page.get("DeleteMarkers", []):
            delete_keys.append({"Key": marker["Key"], "VersionId": marker["VersionId"]})
        if delete_keys:
            client.delete_objects(Bucket=bucket_name, Delete={"Objects": delete_keys})


def delete_bucket_with_contents(client, bucket_name: str):
    """バケットを中身ごと削除（存在しなければ no-op）"""
    if not bucket_exists(client, bucket_name):
        return
    empty_bucket(client, bucket_name)
    client.delete_bucket(Bucket=bucket_name)
