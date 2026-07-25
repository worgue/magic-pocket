"""DSQL リソースの boto3 呼び出しに関する回帰テスト。

boto3 dsql service model のパラメータは **lowerCamel** (identifier /
resourceArn) であり、PascalCase (Identifier / ResourceArn) を渡すと
botocore が ParamValidationError を送出する。過去に PascalCase で呼んで
おり初回/再 deploy が常に失敗したため、実 service model で検証する
Stubber を使って casing を固定する。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from botocore.stub import Stubber
from click.testing import CliRunner
from pocket_cli.cli import dsql_cli
from pocket_cli.resources import dsql as dsql_module
from pocket_cli.resources.dsql import Dsql

from pocket.context import DsqlContext

REGION = "us-east-1"
TAG_NAME = "test-dsql"
ARN = "arn:aws:dsql:us-east-1:123456789012:cluster/abc123"
CREATED = datetime(2026, 7, 9, tzinfo=timezone.utc)
ENDPOINT_SECRET_NAME = "/test-testprj-pocket-user/dsql_endpoint"


def _get_cluster_response(identifier: str, status: str = "ACTIVE") -> dict:
    return {
        "identifier": identifier,
        "arn": ARN,
        "status": status,
        "creationTime": CREATED,
        "deletionProtectionEnabled": False,
    }


def _make_dsql(endpoint_secret_name: str = "") -> tuple[Dsql, Stubber]:
    context = DsqlContext(
        region=REGION,
        tag_name=TAG_NAME,
        endpoint_secret_name=endpoint_secret_name,
        endpoint_secret_store="ssm",
    )
    dsql = Dsql(context)
    stubber = Stubber(dsql._client)
    return dsql, stubber


class _StoreRecorder:
    """secret_store の read/put/delete 呼び出しを捕捉する test double。

    boto3 client を関数内で都度生成する secret_store 実装のため、Stubber ではなく
    dsql module に import された関数名を monkeypatch で差し替える。
    """

    def __init__(self, monkeypatch, stored: str | None = None):
        self.stored = stored
        self.puts: list[tuple[str, str, str, str]] = []
        self.deletes: list[str] = []
        monkeypatch.setattr(dsql_module, "read_stored_value", self._read)
        monkeypatch.setattr(dsql_module, "put_stored_value", self._put)
        monkeypatch.setattr(dsql_module, "delete_stored_value", self._delete)

    def _read(self, name, store, region, **kwargs):
        return self.stored

    def _put(self, name, store, value, region):
        self.puts.append((name, store, value, region))

    def _delete(self, name, store, region, **kwargs):
        self.deletes.append(name)


def test_cluster_lookup_uses_lowercamel_params():
    """cluster 探索 (list_clusters → get_cluster → list_tags_for_resource)。

    expected_params を lowerCamel で登録する。実装が PascalCase を渡すと
    botocore が ParamValidationError を送出しテストが失敗する。
    """
    dsql, stubber = _make_dsql()
    stubber.add_response(
        "list_clusters", {"clusters": [{"identifier": "abc123", "arn": ARN}]}
    )
    stubber.add_response(
        "get_cluster",
        _get_cluster_response("abc123"),
        {"identifier": "abc123"},
    )
    stubber.add_response(
        "list_tags_for_resource",
        {"tags": {"Name": TAG_NAME}},
        {"resourceArn": ARN},
    )
    with stubber:
        cluster = dsql.cluster

    assert cluster is not None
    assert cluster["identifier"] == "abc123"
    assert dsql.status == "COMPLETED"
    stubber.assert_no_pending_responses()


def test_cluster_lookup_skips_non_matching_tag():
    """Name タグが一致しないクラスターは None を返す (探索は継続)。"""
    dsql, stubber = _make_dsql()
    stubber.add_response(
        "list_clusters", {"clusters": [{"identifier": "other", "arn": ARN}]}
    )
    stubber.add_response(
        "get_cluster",
        _get_cluster_response("other"),
        {"identifier": "other"},
    )
    stubber.add_response(
        "list_tags_for_resource",
        {"tags": {"Name": "someone-else"}},
        {"resourceArn": ARN},
    )
    with stubber:
        assert dsql.cluster is None
        assert dsql.status == "NOEXIST"
    stubber.assert_no_pending_responses()


def test_delete_uses_lowercamel_params(monkeypatch):
    """delete (delete_cluster → _wait_deleted の get_cluster) の casing。"""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    dsql, stubber = _make_dsql()
    # identifier プロパティ解決のための cluster 探索
    stubber.add_response(
        "list_clusters", {"clusters": [{"identifier": "abc123", "arn": ARN}]}
    )
    stubber.add_response(
        "get_cluster",
        _get_cluster_response("abc123"),
        {"identifier": "abc123"},
    )
    stubber.add_response(
        "list_tags_for_resource",
        {"tags": {"Name": TAG_NAME}},
        {"resourceArn": ARN},
    )
    stubber.add_response(
        "delete_cluster",
        {
            "identifier": "abc123",
            "arn": ARN,
            "status": "DELETING",
            "creationTime": CREATED,
        },
        {"identifier": "abc123"},
    )
    stubber.add_client_error(
        "get_cluster",
        service_error_code="ResourceNotFoundException",
        expected_params={"identifier": "abc123"},
    )
    with stubber:
        dsql.delete()
    stubber.assert_no_pending_responses()


def test_publish_endpoint_writes_to_canonical_path(monkeypatch):
    """ensure_post_deploy_state が endpoint を正準パスへ publish する。"""
    store = _StoreRecorder(monkeypatch, stored=None)
    dsql, _ = _make_dsql(ENDPOINT_SECRET_NAME)
    dsql.__dict__["cluster"] = _get_cluster_response("abc123")
    dsql.ensure_post_deploy_state()
    assert store.puts == [
        (
            ENDPOINT_SECRET_NAME,
            "ssm",
            f"abc123.dsql.{REGION}.on.aws",
            REGION,
        )
    ]


def test_publish_endpoint_skips_write_when_unchanged(monkeypatch):
    """値が既に一致していれば書き込まない (SSM version の増殖防止)。"""
    endpoint = f"abc123.dsql.{REGION}.on.aws"
    store = _StoreRecorder(monkeypatch, stored=endpoint)
    dsql, _ = _make_dsql(ENDPOINT_SECRET_NAME)
    dsql.__dict__["cluster"] = _get_cluster_response("abc123")
    dsql.ensure_post_deploy_state()
    assert store.puts == []


def test_publish_endpoint_noop_without_name_or_cluster(monkeypatch):
    """publish 先未設定 (直接構築の context) や cluster 不在では何もしない。"""
    store = _StoreRecorder(monkeypatch, stored=None)
    dsql, _ = _make_dsql()  # endpoint_secret_name = ""
    dsql.__dict__["cluster"] = _get_cluster_response("abc123")
    dsql.ensure_post_deploy_state()
    dsql2, _ = _make_dsql(ENDPOINT_SECRET_NAME)
    dsql2.__dict__["cluster"] = None
    dsql2.ensure_post_deploy_state()
    assert store.puts == []


def test_delete_unpublishes_endpoint(monkeypatch):
    """cluster 削除で publish 済み endpoint も削除する (対称操作)。"""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    store = _StoreRecorder(monkeypatch)
    dsql, stubber = _make_dsql(ENDPOINT_SECRET_NAME)
    dsql.__dict__["cluster"] = _get_cluster_response("abc123")
    stubber.add_response(
        "delete_cluster",
        {
            "identifier": "abc123",
            "arn": ARN,
            "status": "DELETING",
            "creationTime": CREATED,
        },
        {"identifier": "abc123"},
    )
    stubber.add_client_error(
        "get_cluster",
        service_error_code="ResourceNotFoundException",
        expected_params={"identifier": "abc123"},
    )
    with stubber:
        dsql.delete()
    stubber.assert_no_pending_responses()
    assert store.deletes == [ENDPOINT_SECRET_NAME]


def test_endpoint_cli_json_outputs_to_stdout(monkeypatch):
    """`endpoint --format json` は stdout に装飾なしの JSON だけを出す。

    診断メッセージ (echo.*) は stderr なので、`$(pocket resource dsql
    endpoint --format json)` のような capture が汚れないことを固定する。
    """
    dsql, _ = _make_dsql()
    dsql.__dict__["cluster"] = _get_cluster_response("abc123")
    monkeypatch.setattr(dsql_cli, "_get_dsql_resource", lambda stage: dsql)
    runner = CliRunner()
    result = runner.invoke(
        dsql_cli.dsql, ["endpoint", "--stage", "dev", "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == {
        "endpoint": f"abc123.dsql.{REGION}.on.aws",
        "region": REGION,
        "port": 5432,
    }


def test_endpoint_cli_json_cluster_not_found_exits_nonzero(monkeypatch):
    """json では cluster 不在を exit 1 で伝える (text は warning + exit 0)。"""
    dsql, _ = _make_dsql()
    dsql.__dict__["cluster"] = None
    monkeypatch.setattr(dsql_cli, "_get_dsql_resource", lambda stage: dsql)
    runner = CliRunner()
    result = runner.invoke(
        dsql_cli.dsql, ["endpoint", "--stage", "dev", "--format", "json"]
    )
    assert result.exit_code == 1
    assert result.stdout == ""  # stdout は空のまま (エラーは stderr)
    assert "Cluster not found" in result.stderr

    # text (デフォルト) は従来挙動: exit 0 で stdout は汚さない
    result = runner.invoke(dsql_cli.dsql, ["endpoint", "--stage", "dev"])
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
