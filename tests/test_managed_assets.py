"""managed_assets のアップロードに関するテスト。

managed_assets は CFn stack のライフサイクルとは独立した「ファイル同期」処理として
deploy フローの専用ステップ (deploy_cli.upload_managed_assets) で実行される。
"""

import boto3
from moto import mock_aws
from pocket_cli.cli.deploy_cli import upload_managed_assets
from pocket_cli.resources.cloudfront import CloudFront
from pocket_cli.resources.s3 import S3

from pocket.context import Context


def _make_cf_with_assets(tmp_path, stage="dev"):
    context = Context.from_toml(stage=stage)
    assert context.cloudfront
    cf_ctx = list(context.cloudfront.values())[0]
    asset_dir = tmp_path / "default"
    asset_dir.mkdir()
    (asset_dir / "favicon.ico").write_text("dummy-favicon")
    cf_ctx_with_assets = cf_ctx.model_copy(update={"managed_assets": str(tmp_path)})
    return CloudFront(cf_ctx_with_assets)


@mock_aws
def test_upload_managed_assets_uploads_files(use_toml, tmp_path):
    """ローカル managed_assets が S3 にアップロードされること。"""
    use_toml("tests/data/toml/default.toml")
    cf = _make_cf_with_assets(tmp_path)
    cf.s3_client.create_bucket(
        Bucket=cf.context.bucket_name,
        CreateBucketConfiguration={"LocationConstraint": cf.context.s3_region},
    )

    cf.upload_managed_assets()

    res = cf.s3_client.list_objects_v2(
        Bucket=cf.context.bucket_name, Prefix="pocket_managed/"
    )
    keys = [obj["Key"] for obj in res.get("Contents", [])]
    assert "pocket_managed/favicon.ico" in keys


@mock_aws
def test_upload_managed_assets_overwrites_existing(use_toml, tmp_path):
    """既存ファイルがローカルで変更されたら次の upload で反映されること。"""
    use_toml("tests/data/toml/default.toml")
    cf = _make_cf_with_assets(tmp_path)
    cf.s3_client.create_bucket(
        Bucket=cf.context.bucket_name,
        CreateBucketConfiguration={"LocationConstraint": cf.context.s3_region},
    )

    cf.upload_managed_assets()

    (tmp_path / "default" / "favicon.ico").write_text("updated-favicon")
    cf.upload_managed_assets()

    obj = cf.s3_client.get_object(
        Bucket=cf.context.bucket_name, Key="pocket_managed/favicon.ico"
    )
    assert obj["Body"].read() == b"updated-favicon"


@mock_aws
def test_upload_managed_assets_deletes_stale_objects(use_toml, tmp_path):
    """ローカルから消えたファイルは S3 からも削除されること。"""
    use_toml("tests/data/toml/default.toml")
    cf = _make_cf_with_assets(tmp_path)
    cf.s3_client.create_bucket(
        Bucket=cf.context.bucket_name,
        CreateBucketConfiguration={"LocationConstraint": cf.context.s3_region},
    )

    asset_dir = tmp_path / "default"
    (asset_dir / "robots.txt").write_text("User-agent: *")
    cf.upload_managed_assets()

    (asset_dir / "robots.txt").unlink()
    cf.upload_managed_assets()

    res = cf.s3_client.list_objects_v2(
        Bucket=cf.context.bucket_name, Prefix="pocket_managed/"
    )
    keys = [obj["Key"] for obj in res.get("Contents", [])]
    assert "pocket_managed/robots.txt" not in keys
    assert "pocket_managed/favicon.ico" in keys


@mock_aws
def test_deploy_step_skips_resources_without_managed_assets(use_toml, tmp_path):
    """managed_assets が設定されていない CloudFront resource はスキップされること。"""
    use_toml("tests/data/toml/default.toml")
    context = Context.from_toml(stage="dev")
    # 元の toml は managed_assets を設定していないので、何も起きずに完了する
    upload_managed_assets(context)


@mock_aws
def test_initial_deploy_uploads_managed_assets(use_toml, tmp_path):
    """フィードバック再現: 初回 deploy で managed_assets が S3 に同期されること。

    再現するフロー (deploy_cli.deploy の主要 3 段):
        1. deploy_init_resources → CloudFront.deploy_init  (bucket は未作成)
        2. deploy_resources → S3.create  (bucket 作成)
        3. upload_managed_assets  (専用ステップで同期)

    修正前 (b12488d 時点) は 1 で `_upload_managed_assets` が走り NoSuchBucket
    で落ちていた。本テストは 3 段を順に実行して、最終的に S3 に upload される
    ことまでを確認する。
    """
    use_toml("tests/data/toml/default.toml")
    managed_root = tmp_path / "managed_assets"
    asset_dir = managed_root / "default"
    asset_dir.mkdir(parents=True)
    (asset_dir / "favicon.ico").write_text("dummy")

    context = Context.from_toml(stage="dev")
    assert context.cloudfront and context.s3
    cf_name = next(iter(context.cloudfront))
    context.cloudfront[cf_name] = context.cloudfront[cf_name].model_copy(
        update={"managed_assets": str(managed_root)}
    )

    # フェーズ 1: deploy_init (bucket 未作成。修正前はここで NoSuchBucket)
    cf = CloudFront(context.cloudfront[cf_name])
    cf.deploy_init()

    # フェーズ 2: S3 resource で bucket を作成 (deploy_resources 相当)
    s3 = S3(context.s3, cloudfront_contexts=context.cloudfront)
    s3.create()

    # フェーズ 3: 専用ステップで managed_assets を同期
    upload_managed_assets(context)

    s3_client = boto3.client("s3", region_name=context.cloudfront[cf_name].s3_region)
    res = s3_client.list_objects_v2(
        Bucket=context.cloudfront[cf_name].bucket_name, Prefix="pocket_managed/"
    )
    keys = [obj["Key"] for obj in res.get("Contents", [])]
    assert "pocket_managed/favicon.ico" in keys
