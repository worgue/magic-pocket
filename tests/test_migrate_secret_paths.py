"""`pocket migrate secret-paths` の中核 (Mediator.migrate_user_secret_path) のテスト。

stored user secret を旧キー基準パス (/{pocket_key}-user/{key}) から新 type 基準パス
(/{pocket_key}-user/{type}) へ copy→verify→旧 delete する。冪等性を検証する。
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError
from pocket_cli.mediator import Mediator

from pocket import settings
from pocket.context import SecretsContext

OLD = "/test-testprj-pocket-user/DATABASE_URL"  # 旧: キー基準
NEW = "/test-testprj-pocket-user/neon_database_url"  # 新: type 基準


def _ctx_stub(secrets_context) -> Any:
    class _Aws:
        secrets = secrets_context

    class _Ctx:
        awscontainer = _Aws()

    return _Ctx()


def _make_sc(base_settings):
    secrets = settings.Secrets(
        store="ssm",
        user={"DATABASE_URL": settings.UserSecretSpec(type="neon_database_url")},
    )
    return SecretsContext.from_settings(secrets, base_settings)


class _FakeSsm:
    """name→value の状態を持つ SSM stub (copy/delete を追跡できる)。"""

    def __init__(self, store: dict):
        self.store = store

    def get_parameter(self, Name, WithDecryption=False):  # noqa: N803
        if Name not in self.store:
            raise ClientError({"Error": {"Code": "ParameterNotFound"}}, "GetParameter")
        return {"Parameter": {"Value": self.store[Name]}}

    def put_parameter(self, Name, Value, Type, Overwrite):  # noqa: N803
        self.store[Name] = Value

    def delete_parameter(self, Name):  # noqa: N803
        del self.store[Name]


def _setup(base_settings, monkeypatch, store: dict):
    """boto3 を fake で差し替えた Mediator と、対象 spec を返す。"""
    fake = _FakeSsm(store)
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)
    sc = _make_sc(base_settings)
    return Mediator(_ctx_stub(sc)), sc.user["DATABASE_URL"]


def test_migrate_copies_and_deletes_old(base_settings, monkeypatch):
    store = {OLD: "postgres://neon"}
    med, spec = _setup(base_settings, monkeypatch, store)
    info = med.migrate_user_secret_path("DATABASE_URL", spec)
    assert info["status"] == "migrated"
    assert store == {NEW: "postgres://neon"}  # 新へ copy、旧は delete


def test_migrate_idempotent_already_migrated(base_settings, monkeypatch):
    store = {NEW: "postgres://neon"}
    med, spec = _setup(base_settings, monkeypatch, store)
    info = med.migrate_user_secret_path("DATABASE_URL", spec)
    assert info["status"] == "already"
    assert store == {NEW: "postgres://neon"}  # 変化なし


def test_migrate_cleans_up_interrupted(base_settings, monkeypatch):
    # copy 済み・旧 delete 前で中断したケース: 再実行で旧のみ delete
    store = {OLD: "postgres://neon", NEW: "postgres://neon"}
    med, spec = _setup(base_settings, monkeypatch, store)
    info = med.migrate_user_secret_path("DATABASE_URL", spec)
    assert info["status"] == "cleaned"
    assert store == {NEW: "postgres://neon"}


def test_migrate_missing_both(base_settings, monkeypatch):
    store: dict = {}
    med, spec = _setup(base_settings, monkeypatch, store)
    info = med.migrate_user_secret_path("DATABASE_URL", spec)
    assert info["status"] == "missing"
    assert store == {}


def test_migrate_dry_run_does_not_write(base_settings, monkeypatch):
    store = {OLD: "postgres://neon"}
    med, spec = _setup(base_settings, monkeypatch, store)
    info = med.migrate_user_secret_path("DATABASE_URL", spec, dry_run=True)
    assert info["status"] == "would-migrate"
    assert store == {OLD: "postgres://neon"}  # 書込みなし
