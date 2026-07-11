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
from pocket_cli.resources.dsql import Dsql

from pocket.context import DsqlContext

REGION = "us-east-1"
TAG_NAME = "test-dsql"
ARN = "arn:aws:dsql:us-east-1:123456789012:cluster/abc123"
CREATED = datetime(2026, 7, 9, tzinfo=timezone.utc)


def _get_cluster_response(identifier: str, status: str = "ACTIVE") -> dict:
    return {
        "identifier": identifier,
        "arn": ARN,
        "status": status,
        "creationTime": CREATED,
        "deletionProtectionEnabled": False,
    }


def _make_dsql() -> tuple[Dsql, Stubber]:
    context = DsqlContext(region=REGION, tag_name=TAG_NAME)
    dsql = Dsql(context)
    stubber = Stubber(dsql._client)
    return dsql, stubber


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
