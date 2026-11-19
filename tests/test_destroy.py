from unittest import mock

import boto3
import pytest
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
        "_destroy_containers",
        lambda c, with_secrets: calls.append("awscontainer"),
    )
    monkeypatch.setattr(
        destroy_cli, "_destroy_backup", lambda c, yes: calls.append("backup")
    )
    monkeypatch.setattr(destroy_cli, "_destroy_dsql", lambda c: calls.append("dsql"))
    monkeypatch.setattr(destroy_cli, "_destroy_rds", lambda c: calls.append("rds"))
    monkeypatch.setattr(destroy_cli, "_destroy_vpc", lambda c: calls.append("vpc"))

    destroy_cli._destroy_resources(context, with_secrets=True, with_state_bucket=False)

    assert "vpc" in calls
    assert calls.index("awscontainer") < calls.index("vpc")
    assert calls.index("rds") < calls.index("vpc")
    # backup plan は cluster 削除より先に消す (selection が消えた cluster の
    # ARN を指したまま残ると以後の backup job が失敗し続けるため)
    assert calls.index("backup") < calls.index("dsql")
    assert calls.index("backup") < calls.index("rds")


@mock_aws
def test_ecr_info_finds_repo_beyond_default_page_size():
    """repo が 100 超あっても名前指定で対象 repo を見つけること

    describe_repositories の全列挙は既定 100 件で切れるため、
    見落とすと create_repository が AlreadyExists で deploy が落ちる (回帰)。
    """
    client = boto3.client("ecr", region_name=REGION)
    for i in range(101):
        client.create_repository(repositoryName="repo-%03d" % i)
    client.create_repository(repositoryName="target-repo")
    ecr = Ecr(REGION, "target-repo", "latest", "Dockerfile", "linux/amd64")
    assert ecr.exists()


@mock_aws
def test_ecr_image_detail_empty_when_image_missing():
    """対象タグの image が無いとき image_detail が空を返すこと"""
    client = boto3.client("ecr", region_name=REGION)
    client.create_repository(repositoryName="target-repo")
    ecr = Ecr(REGION, "target-repo", "missing-tag", "Dockerfile", "linux/amd64")
    detail = ecr.image_detail
    assert detail.hash is None or not detail.hash


def _delete_failing_stack(failures: int) -> mock.MagicMock:
    """削除が `failures` 回だけ DELETE_FAILED で終わる container stack。"""
    stack = mock.MagicMock()
    stack.name = "dev-testprj-container-main"
    stack.status_detail = "DELETE_FAILED"
    stack.wait_status.side_effect = [
        *[RuntimeError("Stack 'x': DELETE_FAILED")] * failures,
        None,
    ]
    return stack


def test_delete_container_stack_retries_delete_failed_once():
    """API Gateway の ManagedOverrides が内部エラーで消えないときは 1 回やり直す。"""
    stack = _delete_failing_stack(1)
    destroy_cli._delete_container_stack(stack)
    assert stack.delete.call_count == 2
    assert stack.wait_status.call_count == 2


def test_delete_container_stack_stops_after_second_failure():
    stack = _delete_failing_stack(2)
    with pytest.raises(RuntimeError, match="DELETE_FAILED"):
        destroy_cli._delete_container_stack(stack)
    assert stack.delete.call_count == 2


def test_delete_container_stack_does_not_retry_other_failures():
    """DELETE_FAILED 以外 (タイムアウト等) はやり直さない。"""
    stack = _delete_failing_stack(1)
    stack.status_detail = "DELETE_IN_PROGRESS"
    with pytest.raises(RuntimeError):
        destroy_cli._delete_container_stack(stack)
    assert stack.delete.call_count == 1


@mock_aws
def test_destroy_deletes_waf_stack_after_cloudfront(use_toml, monkeypatch):
    """WAF の stack も削除対象 (WebACL は distribution の後でないと消せない)。"""
    use_toml("tests/data/toml_vectors/cloudfront_full.toml")
    context = Context.from_toml(stage="dev")
    with_waf = [name for name, cf in context.cloudfront.items() if cf.waf]
    assert with_waf

    targets = destroy_cli._collect_targets(context, True, False)
    for name in with_waf:
        waf = targets.index("CloudFront WAF '%s' (us-east-1 WebACL)" % name)
        assert (
            targets.index("CloudFront '%s' (CFNスタック + バケットポリシー)" % name)
            < waf
        )

    order: list[str] = []
    for label, cls in [
        ("cloudfront", destroy_cli.CloudFront),
        ("acm", destroy_cli.CloudFrontAcm),
        ("waf", destroy_cli.CloudFrontWaf),
    ]:
        monkeypatch.setattr(cls, "stack", mock.MagicMock(status="COMPLETED"))
        monkeypatch.setattr(
            cls,
            "delete",
            lambda self, label=label: order.append(f"{label} {self.context.name}"),
        )
    destroy_cli._destroy_cloudfront_and_acm(context)
    for name in with_waf:
        assert order.index(f"cloudfront {name}") < order.index(f"waf {name}")
    assert [o for o in order if o.startswith("waf ")] == [
        f"waf {name}" for name in with_waf
    ]
