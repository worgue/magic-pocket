"""managed_assets のアップロードに関するテスト。

CloudFront スタックが既に COMPLETED の場合でも、
deploy_init で managed_assets が再アップロードされることを検証する。
"""

from moto import mock_aws
from pocket_cli.resources.cloudfront import CloudFront

from pocket.context import Context


@mock_aws
def test_deploy_init_uploads_managed_assets(use_toml, tmp_path, monkeypatch):
    """CloudFront.deploy_init() で _upload_managed_assets が呼ばれることを確認"""
    use_toml("tests/data/toml/default.toml")
    context = Context.from_toml(stage="dev")
    assert context.cloudfront

    cf_ctx = list(context.cloudfront.values())[0]
    cf = CloudFront(cf_ctx)

    upload_called = False

    def mock_upload():
        nonlocal upload_called
        upload_called = True

    monkeypatch.setattr(cf, "_upload_managed_assets", mock_upload)

    cf.deploy_init()

    assert upload_called, "deploy_init で _upload_managed_assets が呼ばれるべき"
