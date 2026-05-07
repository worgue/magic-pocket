import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from pocket_cli.resources.s3 import S3

from pocket import settings
from pocket.context import S3Context, S3LifecycleRuleContext

REGION = "ap-southeast-1"
BUCKET = "test-s3-versioning-bucket"


def _ctx(*, versioning: bool = False, lifecycle_rules=None) -> S3Context:
    return S3Context(
        region=REGION,
        bucket_name=BUCKET,
        versioning=versioning,
        lifecycle_rules=lifecycle_rules or [],
    )


@pytest.fixture
def base_root_settings():
    return settings.Settings.model_validate(
        {
            "stage": "test",
            "general": {
                "region": REGION,
                "project_name": "testprj",
                "stages": ["test"],
            },
        }
    )


def test_s3_settings_defaults():
    s3 = settings.S3()
    assert s3.versioning is False
    assert s3.lifecycle_rules == []


def test_s3_settings_versioning_and_lifecycle(base_root_settings):
    s3 = settings.S3.model_validate(
        {
            "versioning": True,
            "lifecycle_rules": [
                {
                    "id": "expire-static",
                    "prefix": "static/",
                    "noncurrent_version_expiration_days": 1,
                }
            ],
        }
    )
    ctx = S3Context.from_settings(s3, base_root_settings)
    assert ctx.versioning is True
    assert len(ctx.lifecycle_rules) == 1
    assert ctx.lifecycle_rules[0].id == "expire-static"
    assert ctx.lifecycle_rules[0].prefix == "static/"
    assert ctx.lifecycle_rules[0].noncurrent_version_expiration_days == 1


def test_s3_lifecycle_rule_requires_positive_days():
    with pytest.raises(ValueError):
        settings.S3LifecycleRule(
            id="bad", prefix="x/", noncurrent_version_expiration_days=0
        )


@mock_aws
def test_s3_create_enables_versioning_when_true():
    res = S3(_ctx(versioning=True))
    res.create()
    client = boto3.client("s3", region_name=REGION)
    status = client.get_bucket_versioning(Bucket=BUCKET).get("Status")
    assert status == "Enabled"


@mock_aws
def test_s3_create_leaves_versioning_unset_when_false_on_new_bucket():
    # 新規 bucket + versioning=False は PutBucketVersioning を呼ばず Status 未設定
    res = S3(_ctx(versioning=False))
    res.create()
    client = boto3.client("s3", region_name=REGION)
    status = client.get_bucket_versioning(Bucket=BUCKET).get("Status")
    assert status is None


@mock_aws
def test_s3_versioning_false_suspends_existing_enabled():
    # 既存 Enabled に versioning=False で reconcile すると Suspended になる
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    client.put_bucket_versioning(
        Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"}
    )
    res = S3(_ctx(versioning=False))
    res.update()
    status = client.get_bucket_versioning(Bucket=BUCKET).get("Status")
    assert status == "Suspended"


@mock_aws
def test_s3_versioning_false_noop_when_already_suspended():
    # versioning=False で現状 Suspended のときは何もしない
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    client.put_bucket_versioning(
        Bucket=BUCKET, VersioningConfiguration={"Status": "Suspended"}
    )
    res = S3(_ctx(versioning=False))
    assert res.versioning_require_update is False
    res.update()
    status = client.get_bucket_versioning(Bucket=BUCKET).get("Status")
    assert status == "Suspended"


@mock_aws
def test_s3_versioning_idempotent():
    res = S3(_ctx(versioning=True))
    res.create()
    res.update()  # 2 回目: 既に Enabled なので no-op
    client = boto3.client("s3", region_name=REGION)
    status = client.get_bucket_versioning(Bucket=BUCKET).get("Status")
    assert status == "Enabled"


@mock_aws
def test_s3_create_applies_lifecycle_rules():
    rules = [
        S3LifecycleRuleContext(
            id="expire-static",
            prefix="static/",
            noncurrent_version_expiration_days=1,
        ),
        S3LifecycleRuleContext(
            id="expire-media",
            prefix="media/",
            noncurrent_version_expiration_days=7,
        ),
    ]
    res = S3(_ctx(versioning=True, lifecycle_rules=rules))
    res.create()
    client = boto3.client("s3", region_name=REGION)
    got = client.get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"]
    assert len(got) == 2
    by_id = {r["ID"]: r for r in got}
    assert by_id["expire-static"]["Status"] == "Enabled"
    assert by_id["expire-static"]["Filter"]["Prefix"] == "static/"
    assert by_id["expire-static"]["NoncurrentVersionExpiration"]["NoncurrentDays"] == 1
    assert by_id["expire-media"]["NoncurrentVersionExpiration"]["NoncurrentDays"] == 7


@mock_aws
def test_s3_lifecycle_empty_deletes_existing():
    # lifecycle_rules が空のとき、既存 Lifecycle 設定を削除する
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    client.put_bucket_lifecycle_configuration(
        Bucket=BUCKET,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "manual",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "manual/"},
                    "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
                }
            ]
        },
    )
    res = S3(_ctx(versioning=False, lifecycle_rules=[]))
    res.update()
    with pytest.raises(ClientError) as exc_info:
        client.get_bucket_lifecycle_configuration(Bucket=BUCKET)
    assert exc_info.value.response["Error"]["Code"] == "NoSuchLifecycleConfiguration"


@mock_aws
def test_s3_lifecycle_empty_noop_when_no_existing():
    # lifecycle_rules が空 & 現状なし のときは drift なし
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    res = S3(_ctx(versioning=False, lifecycle_rules=[]))
    assert res.lifecycle_require_update is False
    res.update()  # raise しないこと


@mock_aws
def test_s3_lifecycle_replaces_existing():
    """lifecycle_rules を宣言した場合、既存ルールを上書き (replace) する。"""
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    client.put_bucket_lifecycle_configuration(
        Bucket=BUCKET,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "old",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "old/"},
                    "NoncurrentVersionExpiration": {"NoncurrentDays": 99},
                }
            ]
        },
    )
    rules = [
        S3LifecycleRuleContext(
            id="new", prefix="new/", noncurrent_version_expiration_days=1
        ),
    ]
    res = S3(_ctx(versioning=True, lifecycle_rules=rules))
    res.update()
    got = client.get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"]
    assert len(got) == 1
    assert got[0]["ID"] == "new"


@mock_aws
def test_s3_status_detects_versioning_drift():
    res = S3(_ctx(versioning=True))
    # まだバケットがない
    assert res.status == "NOEXIST"
    # バケットだけ手動作成 (versioning 未設定)
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    res = S3(_ctx(versioning=True))
    assert res.status == "REQUIRE_UPDATE"
    res.update()
    # キャッシュをクリアして再評価
    res = S3(_ctx(versioning=True))
    assert res.status == "COMPLETED"


@mock_aws
def test_s3_status_detects_versioning_false_drift_against_enabled():
    # 既存 Enabled に対し versioning=False は drift と判定される
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    client.put_bucket_versioning(
        Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"}
    )
    res = S3(_ctx(versioning=False))
    assert res.status == "REQUIRE_UPDATE"
    res.update()
    res = S3(_ctx(versioning=False))
    assert res.status == "COMPLETED"


@mock_aws
def test_s3_status_detects_lifecycle_drift():
    rules = [
        S3LifecycleRuleContext(
            id="expire", prefix="x/", noncurrent_version_expiration_days=1
        ),
    ]
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    res = S3(_ctx(versioning=False, lifecycle_rules=rules))
    assert res.status == "REQUIRE_UPDATE"
    res.update()
    res = S3(_ctx(versioning=False, lifecycle_rules=rules))
    assert res.status == "COMPLETED"


@mock_aws
def test_s3_status_detects_lifecycle_drift_against_orphan_rules():
    # 既存に手動 lifecycle ルールがあり、toml 側は宣言なしのとき drift 判定
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    client.put_bucket_lifecycle_configuration(
        Bucket=BUCKET,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "manual",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "manual/"},
                    "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
                }
            ]
        },
    )
    res = S3(_ctx(versioning=False, lifecycle_rules=[]))
    assert res.status == "REQUIRE_UPDATE"
    res.update()
    res = S3(_ctx(versioning=False, lifecycle_rules=[]))
    assert res.status == "COMPLETED"


@mock_aws
def test_s3_cors_empty_deletes_existing():
    # cors 未設定で reconcile したとき、既存 CORS 設定が削除される
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    client.put_bucket_cors(
        Bucket=BUCKET,
        CORSConfiguration={
            "CORSRules": [
                {
                    "AllowedOrigins": ["https://manual.example.com"],
                    "AllowedMethods": ["GET"],
                    "AllowedHeaders": ["*"],
                    "MaxAgeSeconds": 3600,
                }
            ]
        },
    )
    res = S3(_ctx(versioning=False))
    assert res.status == "REQUIRE_UPDATE"
    res.update()
    with pytest.raises(ClientError) as exc_info:
        client.get_bucket_cors(Bucket=BUCKET)
    assert exc_info.value.response["Error"]["Code"] == "NoSuchCORSConfiguration"
