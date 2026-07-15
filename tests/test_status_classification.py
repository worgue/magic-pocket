"""rds / tidb / efs の status 分類の回帰テスト。

一時 status を FAILED / NOEXIST に誤分類すると、deploy の誤中断や
未起動クラスタへの接続が起きる。
"""

from __future__ import annotations

import pytest
from pocket_cli.resources.aws.efs import Efs
from pocket_cli.resources.rds import Rds
from pocket_cli.resources.tidb import Cluster, TiDb

from pocket.context import Context


def _make_rds(use_toml, monkeypatch, cluster_status: str) -> Rds:
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.rds is not None
    rds = Rds(context.rds)
    monkeypatch.setattr(
        Rds, "cluster", property(lambda self: {"Status": cluster_status})
    )
    return rds


@pytest.mark.parametrize(
    "transient",
    ["backing-up", "upgrading", "maintenance", "starting", "modifying"],
)
def test_rds_transient_status_is_progress(use_toml, monkeypatch, transient):
    """Aurora の一時 status が FAILED に分類されないこと (deploy の誤中断防止)"""
    rds = _make_rds(use_toml, monkeypatch, transient)
    assert rds.status == "PROGRESS"


def test_rds_known_failure_status_is_failed(use_toml, monkeypatch):
    rds = _make_rds(use_toml, monkeypatch, "inaccessible-encryption-credentials")
    assert rds.status == "FAILED"


def test_tidb_creating_cluster_is_progress(monkeypatch):
    """CREATING を NOEXIST 扱いにすると中断後の再 deploy が未起動クラスタへ
    接続して失敗する (回帰テスト)"""
    tidb = object.__new__(TiDb)
    fake_context = type("Ctx", (), {"public_key": "pk", "private_key": "sk"})()
    monkeypatch.setattr(tidb, "context", fake_context, raising=False)
    cluster = Cluster(
        id="c1", name="c", status="CREATING", host="h", port=4000, user="u.root"
    )
    monkeypatch.setattr(TiDb, "cluster", property(lambda self: cluster))
    assert tidb.status == "PROGRESS"


def test_efs_wait_status_raises_on_timeout(monkeypatch):
    """EFS の wait_status が時間切れで正常 return せず raise すること"""
    efs = object.__new__(Efs)
    monkeypatch.setattr(Efs, "status", property(lambda self: "PROGRESS"))
    monkeypatch.setattr(efs, "clear_status", lambda: None, raising=False)
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="did not become"):
        efs.wait_status("COMPLETED", timeout=9, interval=3)
