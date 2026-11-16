import json

import boto3
import pytest
from moto import mock_aws
from pocket_cli.resources.aws.state import StateBucketRegionError, StateStore


@mock_aws
def test_ensure_bucket_creates_bucket():
    store = StateStore("test-state-bucket", "ap-southeast-1")
    store.ensure_bucket()

    s3 = boto3.client("s3", region_name="ap-southeast-1")
    response = s3.head_bucket(Bucket="test-state-bucket")
    assert response["ResponseMetadata"]["HTTPStatusCode"] == 200

    pab = s3.get_public_access_block(Bucket="test-state-bucket")[
        "PublicAccessBlockConfiguration"
    ]
    assert pab["BlockPublicAcls"] is True
    assert pab["IgnorePublicAcls"] is True
    assert pab["BlockPublicPolicy"] is True
    assert pab["RestrictPublicBuckets"] is True


@mock_aws
def test_ensure_bucket_us_east_1():
    store = StateStore("test-state-bucket", "us-east-1")
    store.ensure_bucket()

    s3 = boto3.client("s3", region_name="us-east-1")
    response = s3.head_bucket(Bucket="test-state-bucket")
    assert response["ResponseMetadata"]["HTTPStatusCode"] == 200


@mock_aws
def test_ensure_bucket_idempotent():
    store = StateStore("test-state-bucket", "ap-southeast-1")
    store.ensure_bucket()
    store.ensure_bucket()

    s3 = boto3.client("s3", region_name="ap-southeast-1")
    response = s3.head_bucket(Bucket="test-state-bucket")
    assert response["ResponseMetadata"]["HTTPStatusCode"] == 200


@mock_aws
def test_load_empty_state():
    store = StateStore("test-state-bucket", "ap-southeast-1")
    store.ensure_bucket()

    state = store.load()
    assert state == {"version": 1, "resources": {}}


@mock_aws
def test_save_and_load():
    store = StateStore("test-state-bucket", "ap-southeast-1")
    store.ensure_bucket()

    store.load()
    store.save()

    store2 = StateStore("test-state-bucket", "ap-southeast-1")
    state = store2.load()
    assert state == {"version": 1, "resources": {}}


@mock_aws
def test_record_single():
    store = StateStore("test-state-bucket", "ap-southeast-1")
    store.ensure_bucket()

    store.record({"s3": {"bucket_name": "my-bucket"}})

    s3 = boto3.client("s3", region_name="ap-southeast-1")
    body = s3.get_object(Bucket="test-state-bucket", Key="resources.json")["Body"]
    state = json.loads(body.read().decode("utf-8"))
    assert state["resources"]["s3"]["bucket_name"] == "my-bucket"


@mock_aws
def test_record_merge():
    store = StateStore("test-state-bucket", "ap-southeast-1")
    store.ensure_bucket()

    store.record({"s3": {"bucket_name": "my-bucket"}})
    store.record({"ecr": {"repository_name": "my-ecr"}})

    state = store.load()
    assert state["resources"]["s3"]["bucket_name"] == "my-bucket"
    assert state["resources"]["ecr"]["repository_name"] == "my-ecr"


@mock_aws
def test_record_merge_nested():
    store = StateStore("test-state-bucket", "ap-southeast-1")
    store.ensure_bucket()

    store.record({"cloudformation": {"container": {"stack_name": "my-stack"}}})
    store.record({"cloudformation": {"vpc": {"stack_name": "my-vpc-stack"}}})

    state = store.load()
    cf = state["resources"]["cloudformation"]
    assert cf["container"]["stack_name"] == "my-stack"
    assert cf["vpc"]["stack_name"] == "my-vpc-stack"


@mock_aws
def test_ensure_bucket_rejects_bucket_in_other_region():
    """region 移設で旧 region に残った state bucket は吸収せず止める"""
    s3 = boto3.client("s3", region_name="ap-northeast-1")
    s3.create_bucket(
        Bucket="test-state-bucket",
        CreateBucketConfiguration={"LocationConstraint": "ap-northeast-1"},
    )
    store = StateStore("test-state-bucket", "ap-southeast-1")
    with pytest.raises(StateBucketRegionError) as e:
        store.ensure_bucket()
    message = str(e.value)
    assert "ap-northeast-1 にありますが、[general] region は ap-southeast-1" in message
    assert "#state-bucket-region" in message
