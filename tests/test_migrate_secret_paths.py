"""`pocket migrate secret-paths` の中核 (_migrate_user_secret_path) のテスト。

stored user secret を旧キー基準パス (/{pocket_key}-user/{key}) から新 type 基準パス
(/{pocket_key}-user/{type}) へ copy→verify→旧 delete する。冪等性を検証する。
"""

from __future__ import annotations

from botocore.exceptions import ClientError
from pocket_cli.cli import migrate_cli

from pocket import settings
from pocket.context import SecretsContext

OLD = "/test-testprj-pocket-user/DATABASE_URL"  # 旧: キー基準
NEW = "/test-testprj-pocket-user/neon_database_url"  # 新: type 基準


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
    """boto3 を fake で差し替えた SecretsContext と、対象 spec を返す。"""
    fake = _FakeSsm(store)
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)
    sc = _make_sc(base_settings)
    return sc, sc.user["DATABASE_URL"]


def test_migrate_copies_and_deletes_old(base_settings, monkeypatch):
    store = {OLD: "postgres://neon"}
    sc, spec = _setup(base_settings, monkeypatch, store)
    info = migrate_cli._migrate_user_secret_path(sc, "DATABASE_URL", spec)
    assert info["status"] == "migrated"
    assert store == {NEW: "postgres://neon"}  # 新へ copy、旧は delete


def test_migrate_idempotent_already_migrated(base_settings, monkeypatch):
    store = {NEW: "postgres://neon"}
    sc, spec = _setup(base_settings, monkeypatch, store)
    info = migrate_cli._migrate_user_secret_path(sc, "DATABASE_URL", spec)
    assert info["status"] == "already"
    assert store == {NEW: "postgres://neon"}  # 変化なし


def test_migrate_cleans_up_interrupted(base_settings, monkeypatch):
    # copy 済み・旧 delete 前で中断したケース: 再実行で旧のみ delete
    store = {OLD: "postgres://neon", NEW: "postgres://neon"}
    sc, spec = _setup(base_settings, monkeypatch, store)
    info = migrate_cli._migrate_user_secret_path(sc, "DATABASE_URL", spec)
    assert info["status"] == "cleaned"
    assert store == {NEW: "postgres://neon"}


def test_migrate_missing_both(base_settings, monkeypatch):
    store: dict = {}
    sc, spec = _setup(base_settings, monkeypatch, store)
    info = migrate_cli._migrate_user_secret_path(sc, "DATABASE_URL", spec)
    assert info["status"] == "missing"
    assert store == {}


def test_migrate_dry_run_does_not_write(base_settings, monkeypatch):
    store = {OLD: "postgres://neon"}
    sc, spec = _setup(base_settings, monkeypatch, store)
    info = migrate_cli._migrate_user_secret_path(sc, "DATABASE_URL", spec, dry_run=True)
    assert info["status"] == "would-migrate"
    assert store == {OLD: "postgres://neon"}  # 書込みなし


# --- CLI フロー: 旧パス削除前の runtime bump 警告 --------------------------------

from types import SimpleNamespace  # noqa: E402


def _patch_secret_paths_flow(monkeypatch, *, plan_status: str):
    """_run_secret_paths を AWS 無しで走らせるための最小 stub 群を仕込む。"""
    spec = SimpleNamespace(type="neon_database_url", store="ssm")
    sc = SimpleNamespace(user={"DATABASE_URL": spec})
    context = SimpleNamespace(awscontainer=SimpleNamespace(secrets=sc))
    monkeypatch.setattr(
        migrate_cli.Context, "from_toml", classmethod(lambda cls, *, stage: context)
    )

    monkeypatch.setattr(
        migrate_cli,
        "_migrate_user_secret_path",
        lambda sc, key, spec, *, dry_run=False: {
            "status": plan_status,
            "key": "DATABASE_URL",
            "type": "neon_database_url",
            "old": OLD,
            "new": NEW,
        },
    )


def test_secret_paths_warns_runtime_bump_before_acting(monkeypatch, capsys):
    """移設対象 (would-migrate) がある場合、旧削除で古い runtime が壊れる旨と

    runtime bump + 再デプロイを促す警告を stderr に出す (dry-run でも出す)。
    """
    _patch_secret_paths_flow(monkeypatch, plan_status="would-migrate")
    migrate_cli._run_secret_paths("sandbox", yes=True, dry_run=True)
    # Rich console は幅 80 で soft-wrap するため、改行・空白を潰して部分一致で見る。
    compact = capsys.readouterr().err.replace("\n", "").replace(" ", "")
    assert "magic-pocket[django]" in compact
    assert "INIT" in compact
    assert "--stage=sandbox" in compact  # 再デプロイ手順に stage が入る


def test_secret_paths_no_warning_when_nothing_to_migrate(monkeypatch, capsys):
    """移設対象が無い (already) 場合は runtime bump 警告を出さない。"""
    _patch_secret_paths_flow(monkeypatch, plan_status="already")
    migrate_cli._run_secret_paths("sandbox", yes=True, dry_run=True)
    compact = capsys.readouterr().err.replace("\n", "").replace(" ", "")
    assert "magic-pocket[django]" not in compact
