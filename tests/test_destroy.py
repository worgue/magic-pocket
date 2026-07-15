import boto3
from moto import mock_aws
from pocket_cli.cli import destroy_cli
from pocket_cli.resources.aws.ecr import Ecr
from pocket_cli.resources.aws.s3_utils import (
    bucket_exists,
    create_bucket,
    delete_bucket_with_contents,
    empty_bucket,
)
from pocket_cli.resources.aws.state import StateStore

from pocket.context import Context

REGION = "ap-southeast-1"


@mock_aws
def test_empty_bucket():
    client = boto3.client("s3", region_name=REGION)
    create_bucket(client, "test-bucket", REGION)
    client.put_object(Bucket="test-bucket", Key="file1.txt", Body=b"hello")
    client.put_object(Bucket="test-bucket", Key="file2.txt", Body=b"world")
    client.put_object(Bucket="test-bucket", Key="dir/file3.txt", Body=b"nested")

    # バケットにオブジェクトがあることを確認
    objects = client.list_objects_v2(Bucket="test-bucket")
    assert objects["KeyCount"] == 3

    empty_bucket(client, "test-bucket")

    # バケットは存在するが中身は空
    assert bucket_exists(client, "test-bucket")
    objects = client.list_objects_v2(Bucket="test-bucket")
    assert objects["KeyCount"] == 0


@mock_aws
def test_delete_bucket_with_contents():
    client = boto3.client("s3", region_name=REGION)
    create_bucket(client, "test-bucket", REGION)
    client.put_object(Bucket="test-bucket", Key="file1.txt", Body=b"hello")
    client.put_object(Bucket="test-bucket", Key="file2.txt", Body=b"world")

    assert bucket_exists(client, "test-bucket")

    delete_bucket_with_contents(client, "test-bucket")

    assert not bucket_exists(client, "test-bucket")


@mock_aws
def test_delete_nonexistent_bucket():
    """存在しないバケットの削除は no-op"""
    client = boto3.client("s3", region_name=REGION)
    # エラーなく完了すること
    delete_bucket_with_contents(client, "nonexistent-bucket")


@mock_aws
def test_ecr_delete():
    ecr = Ecr(REGION, "test-repo", "latest", "Dockerfile", "linux/amd64")
    ecr.create()
    assert ecr.exists()

    ecr.delete()
    assert not ecr.exists()


@mock_aws
def test_ecr_delete_nonexistent():
    """存在しない ECR リポジトリの削除は no-op"""
    ecr = Ecr(REGION, "nonexistent-repo", "latest", "Dockerfile", "linux/amd64")
    assert not ecr.exists()
    # エラーなく完了すること
    ecr.delete()


@mock_aws
def test_state_delete_bucket():
    store = StateStore("test-state-bucket", REGION)
    store.ensure_bucket()
    store.record({"s3": {"bucket_name": "my-bucket"}})

    client = boto3.client("s3", region_name=REGION)
    assert bucket_exists(client, "test-state-bucket")

    store.delete_bucket()

    assert not bucket_exists(client, "test-state-bucket")


def test_destroy_resources_deletes_vpc_after_rds(use_toml, monkeypatch):
    """VPC の削除が AwsContainer / RDS の削除より後に実行されること

    RDS は VPC の subnet / SG を使用しているため、先に VPC を消すと
    DELETE_FAILED になり destroy 全体が中断する (回帰テスト)。
    """
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")

    calls: list[str] = []
    monkeypatch.setattr(
        destroy_cli,
        "_destroy_cloudfront_and_acm",
        lambda c: calls.append("cloudfront"),
    )
    monkeypatch.setattr(
        destroy_cli,
        "_destroy_awscontainer",
        lambda c, with_secrets: calls.append("awscontainer"),
    )
    monkeypatch.setattr(destroy_cli, "_destroy_dsql", lambda c: calls.append("dsql"))
    monkeypatch.setattr(destroy_cli, "_destroy_rds", lambda c: calls.append("rds"))
    monkeypatch.setattr(destroy_cli, "_destroy_vpc", lambda c: calls.append("vpc"))

    destroy_cli._destroy_resources(context, with_secrets=True, with_state_bucket=False)

    assert "vpc" in calls
    assert calls.index("awscontainer") < calls.index("vpc")
    assert calls.index("rds") < calls.index("vpc")
