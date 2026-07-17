"""`pocket <db> store-url` と関連ヘルパーのテスト。

検証対象:
- StoredUserSecretStore.put: stored user secret 正準名への put (ssm / sm 新規・既存)
- StoredUserSecretStore.exists: 存在判定
- run_store_url: 対象特定 (単一 / --key / 0 件 / 複数) と既存時 no-op / --force 上書き
- computed (managed db url type) の deprecation warning
"""

from __future__ import annotations

from typing import Any

import click
import pytest
from botocore.exceptions import ClientError
from pocket_cli.cli import store_url_helper
from pocket_cli.mediator import Mediator
from pydantic import ValidationError

from pocket import settings
from pocket.context import SecretsContext


def _ctx_stub(secrets_context) -> Any:
    class _Aws:
        secrets = secrets_context

    class _Ctx:
        awscontainer = _Aws()

    return _Ctx()


def _make_sc(base_settings, store, types: dict):
    user = {k: settings.UserSecretSpec(type=t) for k, t in types.items()}
    secrets = settings.Secrets(store=store, user=user)
    return SecretsContext.from_settings(secrets, base_settings)


class _FakeAws:
    """存在確認 (get_parameter/describe_secret) と put を 1 つで扱う boto3 stub。"""

    def __init__(self, exists: bool = False):
        self.exists = exists
        self.puts: list[tuple[str, str]] = []
        self.create_calls: list[str] = []
        self.put_value_calls: list[str] = []

    def get_parameter(self, Name):  # noqa: N803
        if not self.exists:
            raise ClientError({"Error": {"Code": "ParameterNotFound"}}, "GetParameter")
        return {"Parameter": {"Value": "x"}}

    def describe_secret(self, SecretId):  # noqa: N803
        if not self.exists:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException"}}, "DescribeSecret"
            )
        return {"ARN": "x"}

    def put_parameter(self, Name, Value, Type, Overwrite):  # noqa: N803
        self.puts.append((Name, Value))

    def create_secret(self, Name, SecretString, Tags):  # noqa: N803
        if self.exists:
            raise ClientError(
                {"Error": {"Code": "ResourceExistsException"}}, "CreateSecret"
            )
        self.create_calls.append(Name)
        self.puts.append((Name, SecretString))

    def put_secret_value(self, SecretId, SecretString):  # noqa: N803
        self.put_value_calls.append(SecretId)
        self.puts.append((SecretId, SecretString))


# --- StoredUserSecretStore.put -------------------------------------------------


def test_store_user_secret_ssm_put_parameter(base_settings, monkeypatch):
    sc = _make_sc(base_settings, "ssm", {"DATABASE_URL": "neon_database_url"})
    fake = _FakeAws()
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)
    sc.user_store.put(sc.user["DATABASE_URL"], "postgres://u:p@h:5432/db")
    assert fake.puts == [
        ("/test-testprj-pocket-user/neon_database_url", "postgres://u:p@h:5432/db")
    ]


def test_store_user_secret_sm_create(base_settings, monkeypatch):
    sc = _make_sc(base_settings, "sm", {"DATABASE_URL": "neon_database_url"})
    fake = _FakeAws(exists=False)
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)
    sc.user_store.put(sc.user["DATABASE_URL"], "url")
    assert fake.create_calls == ["test-testprj-pocket-user/neon_database_url"]
    assert fake.put_value_calls == []


def test_store_user_secret_sm_existing_uses_put_secret_value(
    base_settings, monkeypatch
):
    sc = _make_sc(base_settings, "sm", {"DATABASE_URL": "neon_database_url"})
    fake = _FakeAws(exists=True)
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)
    sc.user_store.put(sc.user["DATABASE_URL"], "url")
    assert fake.put_value_calls == ["test-testprj-pocket-user/neon_database_url"]
    assert fake.create_calls == []


# --- run_store_url 対象特定 ---------------------------------------------------


def _patch_from_toml(monkeypatch, ctx):
    monkeypatch.setattr(store_url_helper.Context, "from_toml", lambda stage: ctx)


def test_run_store_url_single_candidate_puts(base_settings, monkeypatch):
    sc = _make_sc(base_settings, "ssm", {"DATABASE_URL": "neon_database_url"})
    fake = _FakeAws(exists=False)
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)
    _patch_from_toml(monkeypatch, _ctx_stub(sc))

    called = {}

    def ensure(context):
        called["yes"] = True
        return "postgres://u:p@h:5432/db"

    store_url_helper.run_store_url(
        stage="dev",
        secret_type="neon_database_url",
        db_label="Neon",
        key=None,
        force=False,
        ensure_and_compute_url=ensure,
    )
    assert called.get("yes")
    assert fake.puts == [
        ("/test-testprj-pocket-user/neon_database_url", "postgres://u:p@h:5432/db")
    ]


def test_run_store_url_existing_noop_without_force(base_settings, monkeypatch):
    sc = _make_sc(base_settings, "ssm", {"DATABASE_URL": "neon_database_url"})
    fake = _FakeAws(exists=True)
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)
    _patch_from_toml(monkeypatch, _ctx_stub(sc))

    called = {}

    def ensure(context):
        called["yes"] = True
        return "url"

    store_url_helper.run_store_url(
        stage="dev",
        secret_type="neon_database_url",
        db_label="Neon",
        key=None,
        force=False,
        ensure_and_compute_url=ensure,
    )
    assert "yes" not in called  # ensure (リソース ensure / API) は走らない
    assert fake.puts == []


def test_run_store_url_force_overwrites_existing(base_settings, monkeypatch):
    sc = _make_sc(base_settings, "ssm", {"DATABASE_URL": "neon_database_url"})
    fake = _FakeAws(exists=True)
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)
    _patch_from_toml(monkeypatch, _ctx_stub(sc))

    store_url_helper.run_store_url(
        stage="dev",
        secret_type="neon_database_url",
        db_label="Neon",
        key=None,
        force=True,
        ensure_and_compute_url=lambda context: "url2",
    )
    assert fake.puts == [("/test-testprj-pocket-user/neon_database_url", "url2")]


def test_run_store_url_no_candidate_raises(base_settings, monkeypatch):
    sc = _make_sc(base_settings, "ssm", {"OTHER": "tidb_database_url"})
    _patch_from_toml(monkeypatch, _ctx_stub(sc))
    with pytest.raises(click.ClickException, match="宣言されていません"):
        store_url_helper.run_store_url(
            stage="dev",
            secret_type="neon_database_url",
            db_label="Neon",
            key=None,
            force=False,
            ensure_and_compute_url=lambda context: "url",
        )


def test_run_store_url_duplicate_type_config_is_rejected(base_settings):
    """同一 type の複数宣言は保存パス衝突のため config 構築時に弾かれる。

    (旧: store-url の --key で振り分けていたが、type 基準パスへの移行で廃止。)
    """
    with pytest.raises(ValidationError):
        settings.Secrets(
            store="ssm",
            user={
                "DB1": settings.UserSecretSpec(type="neon_database_url"),
                "DB2": settings.UserSecretSpec(type="neon_database_url"),
            },
        )


def test_run_store_url_key_selects_declared_target(base_settings, monkeypatch):
    """--key で宣言済みキーを明示指定できる (type 一致・単一宣言)。"""
    sc = _make_sc(base_settings, "ssm", {"DATABASE_URL": "neon_database_url"})
    fake = _FakeAws(exists=False)
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)
    _patch_from_toml(monkeypatch, _ctx_stub(sc))
    store_url_helper.run_store_url(
        stage="dev",
        secret_type="neon_database_url",
        db_label="Neon",
        key="DATABASE_URL",
        force=False,
        ensure_and_compute_url=lambda context: "url",
    )
    # 保存先は type 基準 (キー名に依存しない)
    assert fake.puts == [("/test-testprj-pocket-user/neon_database_url", "url")]


def test_run_store_url_key_type_mismatch_raises(base_settings, monkeypatch):
    sc = _make_sc(base_settings, "ssm", {"DB1": "neon_database_url"})
    _patch_from_toml(monkeypatch, _ctx_stub(sc))
    with pytest.raises(click.ClickException, match="type="):
        store_url_helper.run_store_url(
            stage="dev",
            secret_type="tidb_database_url",
            db_label="TiDB",
            key="DB1",
            force=False,
            ensure_and_compute_url=lambda context: "url",
        )


def test_run_store_url_upstash_single_candidate_puts(base_settings, monkeypatch):
    sc = _make_sc(base_settings, "ssm", {"REDIS_URL": "upstash_redis_url"})
    fake = _FakeAws(exists=False)
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)
    _patch_from_toml(monkeypatch, _ctx_stub(sc))
    store_url_helper.run_store_url(
        stage="dev",
        secret_type="upstash_redis_url",
        db_label="Upstash",
        key=None,
        force=False,
        ensure_and_compute_url=lambda context: "rediss://default:pw@h:6379",
    )
    assert fake.puts == [
        ("/test-testprj-pocket-user/upstash_redis_url", "rediss://default:pw@h:6379")
    ]


# --- computed deprecation warning --------------------------------------------


def test_computed_neon_managed_emits_deprecation_warning(base_settings, monkeypatch):
    secrets = settings.Secrets(
        store="ssm",
        managed={"DATABASE_URL": settings.ManagedSecretSpec(type="neon_database_url")},
    )
    sc = SecretsContext.from_settings(secrets, base_settings)

    class _FakeStore:
        secrets: dict = {}

        def update_secrets(self, d):
            pass

    fake_store = _FakeStore()
    # pocket_store は cached_property。class 側を property で差し替えて boto3 を避ける。
    monkeypatch.setattr(
        type(sc), "pocket_store", property(lambda self: fake_store), raising=False
    )
    mediator = Mediator(_ctx_stub(sc))
    monkeypatch.setattr(Mediator, "_generate_secret", lambda self, spec: "postgres://x")
    warnings: list[str] = []
    monkeypatch.setattr(
        "pocket_cli.mediator.echo.warning", lambda msg: warnings.append(msg)
    )
    mediator.create_pocket_managed_secrets()
    assert any("deprecated" in w for w in warnings)
