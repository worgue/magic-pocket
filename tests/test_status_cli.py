"""`pocket status` の表示まわり (secret の読み込み・マスク) を検証する"""

import json

import boto3
from moto import mock_aws
from pocket_cli.cli import status_cli
from pocket_cli.mediator import Mediator
from pocket_cli.resources.cloudfront import CloudFront
from pocket_cli.resources.cloudfront_keys import CloudFrontKeys
from pocket_cli.resources.s3 import S3

from pocket.context import Context


class _FakeContext:
    def model_dump(self, mode="python"):
        return {"name": "main", "api_key": "plain-value", "nested": {"api_key": None}}


class _FakeResource:
    context = _FakeContext()


def test_show_info_masks_secret_values(capsys):
    """--show-info の JSON は secret をマスクすること (以前は平文で出ていた)"""
    status_cli.show_info_message(_FakeResource())
    out = json.loads(capsys.readouterr().out)
    assert out == {"name": "main", "api_key": "****", "nested": {"api_key": None}}


@mock_aws
def test_load_template_secrets_only_for_secret_backed_stacks(use_toml, monkeypatch):
    """template に secret が入る stack だけ prepare_deploy を呼ぶこと"""
    use_toml("tests/data/toml/cloudfront_signing_key.toml")
    context = Context.from_toml(stage="dev")
    mediator = Mediator(context)
    called = []
    for cls in (CloudFront, CloudFrontKeys):
        monkeypatch.setattr(
            cls, "prepare_deploy", lambda self, m: called.append(type(self).__name__)
        )
    cf_ctx = next(iter(context.cloudfront.values()))
    assert context.s3 is not None
    for resource in (
        CloudFrontKeys(cf_ctx),
        CloudFront(cf_ctx),
        S3(context.s3, cloudfront_contexts=context.cloudfront),
    ):
        status_cli.load_template_secrets(resource, mediator)
    assert called == ["CloudFrontKeys", "CloudFront"]


@mock_aws
def test_load_template_secrets_does_not_create_secrets(use_toml):
    """store に secret が無くても作らないこと (status は読むだけ)"""
    use_toml("tests/data/toml/cloudfront_signing_key.toml")
    context = Context.from_toml(stage="dev")
    mediator = Mediator(context)
    cf_ctx = next(iter(context.cloudfront.values()))
    keys = CloudFrontKeys(cf_ctx)
    status_cli.load_template_secrets(keys, mediator)
    status_cli.load_template_secrets(CloudFront(cf_ctx), mediator)

    assert keys._signing_public_key_pem == ""
    region = cf_ctx.region
    sm = boto3.client("secretsmanager", region_name=region)
    assert sm.list_secrets()["SecretList"] == []
    ssm = boto3.client("ssm", region_name=region)
    assert ssm.describe_parameters()["Parameters"] == []
