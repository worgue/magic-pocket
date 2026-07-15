"""orphan secret 掃除 (delete_secret_keys) のテスト。

「全削除 → 書き戻し」方式は 2 呼び出しの間で中断すると orphan でない
SECRET_KEY / RSA signing key 等まで喪失する (SSM は復旧不可) ため、
orphan キーのみを対象にした削除であることを検証する。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import boto3
from moto import mock_aws
from pocket_cli.mediator import Mediator

from pocket import settings
from pocket.context import SecretsContext

REGION = "ap-southeast-1"


@mock_aws
def test_ssm_delete_secret_keys_only_removes_orphans(base_settings):
    secrets = settings.Secrets(store="ssm")
    sc = SecretsContext.from_settings(secrets, base_settings)
    store = sc.pocket_store
    client = boto3.client("ssm", region_name=REGION)
    prefix = "/%s" % sc.pocket_key
    client.put_parameter(Name=prefix + "/KEEP", Value="v1", Type="SecureString")
    client.put_parameter(Name=prefix + "/ORPHAN", Value="v2", Type="SecureString")
    client.put_parameter(Name=prefix + "/ORPHAN2/pem", Value="v3", Type="SecureString")

    store.delete_secret_keys({"ORPHAN", "ORPHAN2"})

    assert store.secrets == {"KEEP": "v1"}


@mock_aws
def test_sm_delete_secret_keys_rewrites_without_full_delete(base_settings):
    secrets = settings.Secrets(store="sm")
    sc = SecretsContext.from_settings(secrets, base_settings)
    store = sc.pocket_store
    store.update_secrets({"KEEP": "v1", "ORPHAN": "v2"})

    store.delete_secret_keys({"ORPHAN"})

    assert store.secrets == {"KEEP": "v1"}


def test_mediator_cleanup_deletes_only_orphan_keys():
    """mediator の掃除が全削除 (delete_secrets) を経由しないこと"""

    class _FakeStore:
        def __init__(self):
            self.secrets = {"KEEP": "a", "ORPHAN": "b"}
            self.deleted_keys: set[str] | None = None
            self.full_delete_called = False

        def delete_secret_keys(self, keys):
            self.deleted_keys = keys

        def delete_secrets(self):
            self.full_delete_called = True

    fake_store = _FakeStore()
    fake_sc = SimpleNamespace(pocket_store=fake_store, managed={"KEEP": object()})
    ctx: Any = SimpleNamespace(awscontainer=SimpleNamespace(secrets=fake_sc))
    mediator = Mediator(ctx)

    mediator._cleanup_orphaned_secrets()

    assert fake_store.deleted_keys == {"ORPHAN"}
    assert not fake_store.full_delete_called


def test_mediator_cleanup_noop_without_orphans():
    class _FakeStore:
        secrets = {"KEEP": "a"}

        def delete_secret_keys(self, keys):
            raise AssertionError("orphan が無ければ削除 API を呼ばないこと")

    fake_sc = SimpleNamespace(pocket_store=_FakeStore(), managed={"KEEP": object()})
    ctx: Any = SimpleNamespace(awscontainer=SimpleNamespace(secrets=fake_sc))
    Mediator(ctx)._cleanup_orphaned_secrets()
