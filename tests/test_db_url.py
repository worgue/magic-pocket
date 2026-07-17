"""`pocket <db> url` と関連ヘルパー (url_helper / StoredUserSecretStore.read) のテスト。

検証対象:
- StoredUserSecretStore.read: stored user secret 名からの読み取り (ssm/sm/未 provision)
- run_get_url: stored-first (default) / live fallback / --live 明示 / 解決不能時の raise
- _read_stored_url: 複数候補の曖昧エラー
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError
from pocket_cli.cli import url_helper
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
    """get_parameter / get_secret_value を返す or NotFound を投げる boto3 stub。"""

    def __init__(self, value: str | None):
        # value=None のとき「未 provision」= NotFound を投げる
        self.value = value

    def get_parameter(self, Name, WithDecryption=False):  # noqa: N803
        if self.value is None:
            raise ClientError({"Error": {"Code": "ParameterNotFound"}}, "GetParameter")
        return {"Parameter": {"Value": self.value}}

    def get_secret_value(self, SecretId):  # noqa: N803
        if self.value is None:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException"}}, "GetSecretValue"
            )
        return {"SecretString": self.value}


# --- StoredUserSecretStore.read ------------------------------------------------


def test_read_user_secret_ssm_returns_value(base_settings, monkeypatch):
    sc = _make_sc(base_settings, "ssm", {"DATABASE_URL": "neon_database_url"})
    monkeypatch.setattr("boto3.client", lambda *a, **k: _FakeAws("postgres://stored"))
    assert sc.user_store.read(sc.user["DATABASE_URL"]) == "postgres://stored"


def test_read_user_secret_sm_returns_value(base_settings, monkeypatch):
    sc = _make_sc(base_settings, "sm", {"DATABASE_URL": "neon_database_url"})
    monkeypatch.setattr("boto3.client", lambda *a, **k: _FakeAws("postgres://sm"))
    assert sc.user_store.read(sc.user["DATABASE_URL"]) == "postgres://sm"


def test_read_user_secret_missing_returns_none(base_settings, monkeypatch):
    sc = _make_sc(base_settings, "ssm", {"DATABASE_URL": "neon_database_url"})
    monkeypatch.setattr("boto3.client", lambda *a, **k: _FakeAws(None))
    assert sc.user_store.read(sc.user["DATABASE_URL"]) is None


# --- run_get_url -------------------------------------------------------------


def _patch_from_toml(monkeypatch, ctx):
    monkeypatch.setattr(url_helper.Context, "from_toml", lambda stage: ctx)


def test_run_get_url_stored_first_prints_stored_without_live(
    base_settings, monkeypatch, capsys
):
    sc = _make_sc(base_settings, "ssm", {"DATABASE_URL": "neon_database_url"})
    monkeypatch.setattr("boto3.client", lambda *a, **k: _FakeAws("postgres://stored"))
    _patch_from_toml(monkeypatch, _ctx_stub(sc))

    live_calls = []

    url_helper.run_get_url(
        stage="dev",
        secret_type="neon_database_url",
        db_label="Neon",
        live_url=lambda ctx: live_calls.append("live") or "postgres://live",
    )
    out = capsys.readouterr().out
    assert out.strip() == "postgres://stored"
    assert live_calls == []  # stored があれば live (副作用) は呼ばない


def test_run_get_url_falls_back_to_live_when_unprovisioned(
    base_settings, monkeypatch, capsys
):
    sc = _make_sc(base_settings, "ssm", {"DATABASE_URL": "neon_database_url"})
    monkeypatch.setattr("boto3.client", lambda *a, **k: _FakeAws(None))  # 未 provision
    _patch_from_toml(monkeypatch, _ctx_stub(sc))

    url_helper.run_get_url(
        stage="dev",
        secret_type="neon_database_url",
        db_label="Neon",
        live_url=lambda ctx: "postgres://live",
    )
    captured = capsys.readouterr()
    assert captured.out.strip() == "postgres://live"
    assert "live 算出" in captured.err  # 警告は stderr に出る


def test_run_get_url_live_flag_skips_stored(base_settings, monkeypatch, capsys):
    sc = _make_sc(base_settings, "ssm", {"DATABASE_URL": "tidb_database_url"})

    # stored は存在するが --live なので読まない (boto3 を呼んだら失敗させる)
    def _boom(*a, **k):
        raise AssertionError("stored should not be read with --live")

    monkeypatch.setattr("boto3.client", _boom)
    _patch_from_toml(monkeypatch, _ctx_stub(sc))

    url_helper.run_get_url(
        stage="dev",
        secret_type="tidb_database_url",
        db_label="TiDB",
        live_url=lambda ctx: "mysql://live",
        live=True,
    )
    assert capsys.readouterr().out.strip() == "mysql://live"


def test_run_get_url_propagates_live_failure(base_settings, monkeypatch):
    """live 算出の失敗は握らず自然に伝播すること。

    以前は except Exception で「解決できませんでした」に丸め from None で
    traceback も破棄していた (AGENTS.md の方針違反 + 原因情報の喪失)。
    """
    sc = _make_sc(base_settings, "ssm", {"DATABASE_URL": "neon_database_url"})
    monkeypatch.setattr("boto3.client", lambda *a, **k: _FakeAws(None))
    _patch_from_toml(monkeypatch, _ctx_stub(sc))

    def live_url(ctx):
        raise RuntimeError("neon not ready")

    with pytest.raises(RuntimeError, match="neon not ready"):
        url_helper.run_get_url(
            stage="dev",
            secret_type="neon_database_url",
            db_label="Neon",
            live_url=live_url,
        )


def test_duplicate_type_config_rejected(base_settings):
    """同一 type の複数宣言は保存パス衝突のため config 構築時に弾かれる
    (旧: url 解決時の曖昧エラーだったが、validator で前倒しに弾く)。"""
    with pytest.raises(ValidationError):
        _make_sc(
            base_settings,
            "ssm",
            {"DB1": "neon_database_url", "DB2": "neon_database_url"},
        )


def test_run_get_url_reads_stored_by_type_without_declaration(
    base_settings, monkeypatch, capsys
):
    """consumer (DATABASE_URL) が別 backend を指していても、type 基準パスから
    その backend の stored URL を直接引ける (dual-declaration / cutover 後)。"""
    # DATABASE_URL は tidb を指す。neon の consumer 宣言は存在しない。
    sc = _make_sc(base_settings, "ssm", {"DATABASE_URL": "tidb_database_url"})
    monkeypatch.setattr(
        "boto3.client", lambda *a, **k: _FakeAws("postgres://neon-stored")
    )
    _patch_from_toml(monkeypatch, _ctx_stub(sc))

    live_calls = []
    url_helper.run_get_url(
        stage="dev",
        secret_type="neon_database_url",
        db_label="Neon",
        live_url=lambda ctx: live_calls.append("live") or "postgres://live",
    )
    out = capsys.readouterr().out
    assert out.strip() == "postgres://neon-stored"
    assert live_calls == []  # type 基準で引けるので live (副作用) は呼ばない
