from __future__ import annotations

import time

from botocore.exceptions import ClientError

from pocket.utils import echo

# 削除直後の同名 bucket は S3 の削除伝播が終わるまで CreateBucket が
# OperationAborted で拒否される (AWS docs では最大 1 時間程度、実測 20 分前後)。
# region 移設で必ず踏む経路なので CLI 側で待って再試行する (KN1454)。
CREATE_BUCKET_RETRY_INTERVAL = 30
CREATE_BUCKET_RETRY_TIMEOUT = 3600


def bucket_exists(client, bucket_name: str) -> bool:
    """バケットの存在確認。404以外のエラーはClientErrorとして再送出"""
    try:
        client.head_bucket(Bucket=bucket_name)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "404":
            return False
        raise


def create_bucket(
    client,
    bucket_name: str,
    region: str,
    *,
    retry_interval: float = CREATE_BUCKET_RETRY_INTERVAL,
    retry_timeout: float = CREATE_BUCKET_RETRY_TIMEOUT,
):
    """リージョンを考慮してバケットを作成。

    削除伝播待ちの OperationAborted は retry_interval 秒ごとに retry_timeout 秒まで
    再試行する (進捗は stderr)。それ以外の ClientError は即座に再送出する。
    """
    deadline = time.monotonic() + retry_timeout
    while True:
        try:
            _create_bucket_once(client, bucket_name, region)
            return
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "OperationAborted":
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    "bucket '%s' の作成が OperationAborted のまま %d 分以内に成功"
                    "しませんでした。削除直後の同名 bucket は削除の伝播 (最大 1 時間"
                    "程度) が終わるまで再作成できません。時間を置いて再実行して"
                    "ください。" % (bucket_name, retry_timeout // 60)
                ) from e
        echo.warning(
            "bucket '%s' の作成が OperationAborted で拒否されました (削除直後の"
            "同名 bucket は削除の伝播が終わるまで再作成できません)。%d 秒後に"
            "再試行します (最長あと %d 分待ちます)..."
            % (bucket_name, retry_interval, remaining // 60)
        )
        time.sleep(retry_interval)


def _create_bucket_once(client, bucket_name: str, region: str):
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
