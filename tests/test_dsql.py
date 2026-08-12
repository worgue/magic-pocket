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

import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber
from click.testing import CliRunner
from pocket_cli.cli import dsql_cli
from pocket_cli.resources import dsql as dsql_module
from pocket_cli.resources.dsql import Dsql
from pydantic import ValidationError

from pocket.context import DsqlBackupContext, DsqlContext
from pocket.settings import DsqlBackup as DsqlBackupSettings

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
    backup_stubber = Stubber(dsql._backup)
    backup_stubber.add_response("list_backup_plans", {"BackupPlansList": []})
    backup_stubber.activate()
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
    backup_stubber = Stubber(dsql._backup)
    backup_stubber.add_response("list_backup_plans", {"BackupPlansList": []})
    backup_stubber.activate()
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


ROLE_ARN = "arn:aws:iam::123456789012:role/forge-pocket-backup-role"
BOUNDARY_ARN = "arn:aws:iam::123456789012:policy/forge-boundary"


def _iam_role_response(arn: str = ROLE_ARN) -> dict:
    return {
        "Role": {
            "Path": "/",
            "RoleName": "forge-pocket-backup-role",
            "RoleId": "AROAEXAMPLEROLEID",
            "Arn": arn,
            "CreateDate": CREATED,
        }
    }


def test_start_backup_params():
    """start_backup_job のパラメータ (AWS Backup 側は PascalCase)。

    vault / ロールを明示指定した場合は ensure (describe/get_role) を呼ばない。
    """
    dsql, _ = _make_dsql()
    dsql.__dict__["cluster"] = _get_cluster_response("abc123")
    stubber = Stubber(dsql._backup)
    stubber.add_response(
        "start_backup_job",
        {"BackupJobId": "job-1", "CreationDate": CREATED},
        {
            "BackupVaultName": "my-vault",
            "ResourceArn": ARN,
            "IamRoleArn": ROLE_ARN,
            "Lifecycle": {"DeleteAfterDays": 35},
        },
    )
    with stubber:
        job_id = dsql.start_backup("my-vault", iam_role_arn=ROLE_ARN, retention_days=35)
    assert job_id == "job-1"
    stubber.assert_no_pending_responses()


def test_start_backup_ensures_pocket_managed_vault_and_role(monkeypatch):
    """省略時は pocket 管理の vault / ロールを ensure して使う (既存なら noop)。"""
    dsql, _ = _make_dsql()
    dsql.__dict__["cluster"] = _get_cluster_response("abc123")
    backup_stub = Stubber(dsql._backup)
    backup_stub.add_client_error(
        "create_backup_vault",
        service_error_code="AlreadyExistsException",
        expected_params={"BackupVaultName": "pocket-backup"},
    )
    backup_stub.add_response(
        "start_backup_job",
        {"BackupJobId": "job-1", "CreationDate": CREATED},
        {
            "BackupVaultName": "pocket-backup",
            "ResourceArn": ARN,
            "IamRoleArn": ROLE_ARN,
        },
    )
    iam_stub = Stubber(dsql._iam)
    iam_stub.add_response(
        "get_role", _iam_role_response(), {"RoleName": "forge-pocket-backup-role"}
    )
    with backup_stub, iam_stub:
        job_id = dsql.start_backup()
    assert job_id == "job-1"
    backup_stub.assert_no_pending_responses()
    iam_stub.assert_no_pending_responses()


def test_ensure_backup_vault_creates_when_missing():
    """vault 不在なら create-first で作成する (describe は撃たない)。

    vault ゼロのアカウントでは存在しない vault への Describe が
    ResourceNotFoundException ではなく AccessDeniedException になるため、
    describe による存在確認では未作成を判定できない。
    """
    dsql, _ = _make_dsql()
    stubber = Stubber(dsql._backup)
    stubber.add_response(
        "create_backup_vault",
        {"BackupVaultName": "pocket-backup"},
        {"BackupVaultName": "pocket-backup"},
    )
    with stubber:
        dsql._ensure_backup_vault("pocket-backup")
    stubber.assert_no_pending_responses()


def test_ensure_backup_vault_noop_when_exists():
    """既存 vault の AlreadyExistsException は握って noop (冪等)。"""
    dsql, _ = _make_dsql()
    stubber = Stubber(dsql._backup)
    stubber.add_client_error(
        "create_backup_vault",
        service_error_code="AlreadyExistsException",
        expected_params={"BackupVaultName": "pocket-backup"},
    )
    with stubber:
        dsql._ensure_backup_vault("pocket-backup")
    stubber.assert_no_pending_responses()


def test_ensure_backup_vault_raises_on_access_denied():
    """本物の権限不足 (create の AccessDenied) はそのまま raise する。"""
    dsql, _ = _make_dsql()
    stubber = Stubber(dsql._backup)
    stubber.add_client_error(
        "create_backup_vault",
        service_error_code="AccessDeniedException",
        expected_params={"BackupVaultName": "pocket-backup"},
    )
    with stubber, pytest.raises(ClientError):
        dsql._ensure_backup_vault("pocket-backup")
    stubber.assert_no_pending_responses()


def test_ensure_backup_role_creates_with_boundary(monkeypatch):
    """ロール不在なら boundary 付きで作成し managed policy を 2 つ attach する。"""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    context = DsqlContext(
        region=REGION,
        tag_name=TAG_NAME,
        permissions_boundary=BOUNDARY_ARN,
    )
    dsql = Dsql(context)
    stubber = Stubber(dsql._iam)
    stubber.add_client_error(
        "get_role",
        service_error_code="NoSuchEntity",
        expected_params={"RoleName": "forge-pocket-backup-role"},
    )
    stubber.add_response(
        "create_role",
        _iam_role_response(),
        {
            "RoleName": "forge-pocket-backup-role",
            "AssumeRolePolicyDocument": ANY,
            "PermissionsBoundary": BOUNDARY_ARN,
        },
    )
    for policy in (
        "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup",
        "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForRestores",
    ):
        stubber.add_response(
            "attach_role_policy",
            {},
            {"RoleName": "forge-pocket-backup-role", "PolicyArn": policy},
        )
    with stubber:
        arn = dsql._ensure_backup_role()
    assert arn == ROLE_ARN
    stubber.assert_no_pending_responses()


def test_latest_backup_job_picks_newest_by_creation_date():
    """ListBackupJobs の並び順に依存せず CreationDate 最大の job を選ぶ。"""
    dsql, _ = _make_dsql()
    dsql.__dict__["cluster"] = _get_cluster_response("abc123")
    stubber = Stubber(dsql._backup)
    stubber.add_response(
        "list_backup_jobs",
        {
            "BackupJobs": [
                {"BackupJobId": "old", "CreationDate": datetime(2026, 7, 1)},
                {"BackupJobId": "new", "CreationDate": datetime(2026, 8, 1)},
            ]
        },
        {"ByResourceArn": ARN},
    )
    with stubber:
        job = dsql.latest_backup_job()
    assert job is not None and job["BackupJobId"] == "new"
    stubber.assert_no_pending_responses()


def test_backup_cli_prints_status_commands(monkeypatch):
    """backup 開始後にステータス確認コマンド (pocket / aws CLI) を案内する。"""
    dsql, _ = _make_dsql()
    dsql.__dict__["cluster"] = _get_cluster_response("abc123")
    stubber = Stubber(dsql._backup)
    stubber.add_response(
        "start_backup_job", {"BackupJobId": "job-1", "CreationDate": CREATED}
    )
    monkeypatch.setattr(dsql_cli, "_get_dsql_resource", lambda stage: dsql)
    monkeypatch.setattr(Dsql, "_ensure_backup_vault", lambda self, name: None)
    monkeypatch.setattr(Dsql, "_ensure_backup_role", lambda self: ROLE_ARN)
    runner = CliRunner()
    with stubber:
        result = runner.invoke(dsql_cli.dsql, ["backup", "--stage", "dev"])
    assert result.exit_code == 0, result.output
    assert (
        "pocket resource dsql backup-status --stage=dev --job-id=job-1 --watch"
        in result.stderr
    )
    assert (
        "aws backup describe-backup-job --backup-job-id job-1 --region us-east-1"
        in result.stderr
    )


def test_backup_status_watch_waits_until_terminal(monkeypatch):
    """--watch は終端状態 (COMPLETED) まで describe を繰り返して待機する。"""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    dsql, _ = _make_dsql()
    dsql.__dict__["cluster"] = _get_cluster_response("abc123")
    stubber = Stubber(dsql._backup)
    stubber.add_response(
        "describe_backup_job",
        {"BackupJobId": "job-1", "State": "RUNNING", "PercentDone": "50.0"},
        {"BackupJobId": "job-1"},
    )
    stubber.add_response(
        "describe_backup_job",
        {"BackupJobId": "job-1", "State": "RUNNING"},
        {"BackupJobId": "job-1"},
    )
    stubber.add_response(
        "describe_backup_job",
        {
            "BackupJobId": "job-1",
            "State": "COMPLETED",
            "RecoveryPointArn": (
                "arn:aws:backup:us-east-1:123456789012:recovery-point:rp-1"
            ),
        },
        {"BackupJobId": "job-1"},
    )
    monkeypatch.setattr(dsql_cli, "_get_dsql_resource", lambda stage: dsql)
    runner = CliRunner()
    with stubber:
        result = runner.invoke(
            dsql_cli.dsql,
            ["backup-status", "--stage", "dev", "--job-id", "job-1", "--watch"],
        )
    assert result.exit_code == 0, result.output
    assert "Backup completed" in result.stderr
    stubber.assert_no_pending_responses()


def test_backup_watch_fails_on_terminal_failure(monkeypatch):
    """FAILED 等の異常終端は exit 1 で StatusMessage を表示する。"""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    dsql, _ = _make_dsql()
    dsql.__dict__["cluster"] = _get_cluster_response("abc123")
    stubber = Stubber(dsql._backup)
    stubber.add_response(
        "start_backup_job", {"BackupJobId": "job-1", "CreationDate": CREATED}
    )
    stubber.add_response(
        "describe_backup_job",
        {"BackupJobId": "job-1", "State": "FAILED", "StatusMessage": "role missing"},
        {"BackupJobId": "job-1"},
    )
    monkeypatch.setattr(dsql_cli, "_get_dsql_resource", lambda stage: dsql)
    monkeypatch.setattr(Dsql, "_ensure_backup_vault", lambda self, name: None)
    monkeypatch.setattr(Dsql, "_ensure_backup_role", lambda self: ROLE_ARN)
    runner = CliRunner()
    with stubber:
        result = runner.invoke(dsql_cli.dsql, ["backup", "--stage", "dev", "--watch"])
    assert result.exit_code == 1
    assert "FAILED" in result.stderr
    assert "role missing" in result.stderr


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


def test_deploy_init_warns_about_missing_automatic_backup(capsys):
    """DSQL には自動バックアップが無いため、deploy で毎回明示する。

    他の DB backend (neon / tidb / rds) と違い黙って通ると「managed DB だから
    守られている」と誤解されるため、警告と次の一手を出すことを固定する。
    """
    dsql, _ = _make_dsql()
    dsql.deploy_init()
    # rich console が端末幅で折り返すため、改行を潰してから判定する
    err = capsys.readouterr().err.replace("\n", "")
    assert "自動バックアップ" in err
    assert "[dsql.backup]" in err
    assert "pocket resource dsql backup" in err


def _backup_context(**overrides) -> DsqlContext:
    data: dict = {
        "schedule_expression": "cron(0 3 * * ? *)",
        "timezone": "Asia/Tokyo",
        "cold_storage_after_days": 35,
        "delete_after_days": 365,
    }
    data.update(overrides)
    return DsqlContext(
        region=REGION,
        tag_name=TAG_NAME,
        backup=DsqlBackupContext(**data),
    )


def test_backup_settings_defaults_match_nightly_policy():
    """[dsql.backup] の既定は 毎日 3:00 / 35 日で cold / 365 日で削除"""
    backup = DsqlBackupSettings()
    assert backup.cron == "0 3 * * ? *"
    assert backup.timezone == "UTC"
    assert backup.cold_storage_after_days == 35
    assert backup.delete_after_days == 365
    ctx = DsqlBackupContext.from_settings(backup)
    assert ctx.schedule_expression == "cron(0 3 * * ? *)"


def test_backup_settings_rejects_cold_storage_shorter_than_90_days():
    """AWS Backup の cold storage 最低保持期間 (90 日) を settings で弾く"""
    with pytest.raises(ValidationError, match="cold_storage_after_days"):
        DsqlBackupSettings(cold_storage_after_days=35, delete_after_days=100)
    # 境界 (35 + 90) は許容される
    assert (
        DsqlBackupSettings(
            cold_storage_after_days=35, delete_after_days=125
        ).delete_after_days
        == 125
    )


def test_backup_not_declared_creates_no_plan():
    """[dsql.backup] 未宣言なら plan を一切触らない (API も呼ばない)"""
    dsql, _ = _make_dsql()
    dsql.__dict__["cluster"] = _get_cluster_response("abc123")
    stubber = Stubber(dsql._backup)
    with stubber:  # 応答を登録しない = 呼んだら例外になる
        dsql.ensure_backup_plan()
    stubber.assert_no_pending_responses()


def test_ensure_backup_plan_creates_plan_and_selection(monkeypatch):
    """宣言時は vault / plan / selection を作り、lifecycle と timezone を渡す"""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    dsql = Dsql(_backup_context())
    dsql.__dict__["cluster"] = _get_cluster_response("abc123")
    monkeypatch.setattr(Dsql, "_ensure_backup_role", lambda self: ROLE_ARN)
    stubber = Stubber(dsql._backup)
    stubber.add_client_error(
        "create_backup_vault",
        service_error_code="AlreadyExistsException",
        expected_params={"BackupVaultName": "pocket-backup"},
    )
    stubber.add_response("list_backup_plans", {"BackupPlansList": []})
    stubber.add_response(
        "create_backup_plan",
        {"BackupPlanId": "plan-1"},
        {
            "BackupPlan": {
                "BackupPlanName": "%s-backup" % TAG_NAME,
                "Rules": [
                    {
                        "RuleName": "daily",
                        "TargetBackupVaultName": "pocket-backup",
                        "ScheduleExpression": "cron(0 3 * * ? *)",
                        "ScheduleExpressionTimezone": "Asia/Tokyo",
                        "Lifecycle": {
                            "DeleteAfterDays": 365,
                            "MoveToColdStorageAfterDays": 35,
                        },
                    }
                ],
            }
        },
    )
    stubber.add_response(
        "list_backup_selections",
        {"BackupSelectionsList": []},
        {"BackupPlanId": "plan-1"},
    )
    stubber.add_response(
        "create_backup_selection",
        {"SelectionId": "sel-1"},
        {
            "BackupPlanId": "plan-1",
            "BackupSelection": {
                "SelectionName": "%s-backup" % TAG_NAME,
                "IamRoleArn": ROLE_ARN,
                "Resources": [ARN],
            },
        },
    )
    with stubber:
        dsql.ensure_backup_plan()
    stubber.assert_no_pending_responses()


def test_ensure_backup_plan_is_idempotent(monkeypatch):
    """既存 plan/selection が宣言どおりなら update も再作成もしない。

    AWS が補完する既定値 (StartWindowMinutes 等) は drift 判定に含めない。
    """
    dsql = Dsql(_backup_context())
    dsql.__dict__["cluster"] = _get_cluster_response("abc123")
    monkeypatch.setattr(Dsql, "_ensure_backup_role", lambda self: ROLE_ARN)
    stubber = Stubber(dsql._backup)
    stubber.add_client_error(
        "create_backup_vault",
        service_error_code="AlreadyExistsException",
        expected_params={"BackupVaultName": "pocket-backup"},
    )
    stubber.add_response(
        "list_backup_plans",
        {
            "BackupPlansList": [
                {"BackupPlanId": "plan-1", "BackupPlanName": "%s-backup" % TAG_NAME}
            ]
        },
    )
    stubber.add_response(
        "get_backup_plan",
        {
            "BackupPlan": {
                "BackupPlanName": "%s-backup" % TAG_NAME,
                "Rules": [
                    {
                        "RuleName": "daily",
                        "RuleId": "generated-by-aws",
                        "TargetBackupVaultName": "pocket-backup",
                        "ScheduleExpression": "cron(0 3 * * ? *)",
                        "ScheduleExpressionTimezone": "Asia/Tokyo",
                        "StartWindowMinutes": 60,
                        "CompletionWindowMinutes": 180,
                        "Lifecycle": {
                            "DeleteAfterDays": 365,
                            "MoveToColdStorageAfterDays": 35,
                            "OptInToArchiveForSupportedResources": False,
                        },
                    }
                ],
            }
        },
        {"BackupPlanId": "plan-1"},
    )
    stubber.add_response(
        "list_backup_selections",
        {
            "BackupSelectionsList": [
                {"SelectionId": "sel-1", "SelectionName": "%s-backup" % TAG_NAME}
            ]
        },
        {"BackupPlanId": "plan-1"},
    )
    stubber.add_response(
        "get_backup_selection",
        {
            "BackupSelection": {
                "SelectionName": "%s-backup" % TAG_NAME,
                "IamRoleArn": ROLE_ARN,
                "Resources": [ARN],
            }
        },
        {"BackupPlanId": "plan-1", "SelectionId": "sel-1"},
    )
    with stubber:
        dsql.ensure_backup_plan()
    stubber.assert_no_pending_responses()


def test_deploy_init_skips_warning_when_backup_declared(capsys):
    """[dsql.backup] を宣言した stage では無防備警告を出さない"""
    dsql = Dsql(_backup_context())
    dsql.deploy_init()
    assert capsys.readouterr().err == ""


def test_delete_removes_plan_but_not_recovery_points(monkeypatch):
    """destroy は plan/selection を消すが vault/recovery point は消さない"""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    dsql = Dsql(_backup_context())
    dsql.__dict__["cluster"] = _get_cluster_response("abc123")
    backup_stub = Stubber(dsql._backup)
    backup_stub.add_response(
        "list_backup_plans",
        {
            "BackupPlansList": [
                {"BackupPlanId": "plan-1", "BackupPlanName": "%s-backup" % TAG_NAME}
            ]
        },
    )
    backup_stub.add_response(
        "list_backup_selections",
        {
            "BackupSelectionsList": [
                {"SelectionId": "sel-1", "SelectionName": "%s-backup" % TAG_NAME}
            ]
        },
        {"BackupPlanId": "plan-1"},
    )
    backup_stub.add_response(
        "delete_backup_selection",
        {},
        {"BackupPlanId": "plan-1", "SelectionId": "sel-1"},
    )
    backup_stub.add_response("delete_backup_plan", {}, {"BackupPlanId": "plan-1"})
    dsql_stub = Stubber(dsql._client)
    dsql_stub.add_response(
        "delete_cluster",
        {
            "identifier": "abc123",
            "arn": ARN,
            "status": "DELETING",
            "creationTime": CREATED,
        },
        {"identifier": "abc123"},
    )
    dsql_stub.add_client_error(
        "get_cluster",
        service_error_code="ResourceNotFoundException",
        expected_params={"identifier": "abc123"},
    )
    with backup_stub, dsql_stub:
        dsql.delete()
    # vault / recovery point の削除 API は登録していない = 呼ばれていない
    backup_stub.assert_no_pending_responses()
    dsql_stub.assert_no_pending_responses()


def test_switch_to_cluster_renames_old_before_tagging_new():
    """Name タグが重複する瞬間を作らない (旧を退避名にしてから新に付ける)。

    重複すると cluster 探索がどちらを返すか不定になり、deploy が旧クラスターを
    掴む事故になるため、順序を固定する。
    """
    dsql, stubber = _make_dsql()
    dsql.__dict__["cluster"] = _get_cluster_response("old123")
    new_arn = "arn:aws:dsql:us-east-1:123456789012:cluster/new456"
    stubber.add_response(
        "tag_resource",
        {},
        {"resourceArn": ARN, "tags": {"Name": "%s-replaced-old123" % TAG_NAME}},
    )
    stubber.add_response(
        "tag_resource", {}, {"resourceArn": new_arn, "tags": {"Name": TAG_NAME}}
    )
    with stubber:
        dsql.switch_to_cluster(new_arn)
    stubber.assert_no_pending_responses()


def test_switch_to_cluster_is_idempotent():
    """既に切り替え済み (旧 = 新) なら退避リネームをしない"""
    dsql, stubber = _make_dsql()
    dsql.__dict__["cluster"] = _get_cluster_response("abc123")
    stubber.add_response(
        "tag_resource", {}, {"resourceArn": ARN, "tags": {"Name": TAG_NAME}}
    )
    with stubber:
        dsql.switch_to_cluster(ARN)
    stubber.assert_no_pending_responses()


def test_start_restore_passes_deletion_protection_from_settings(monkeypatch):
    """復元先の削除保護は AWS 既定 (ON) ではなく pocket.toml の値に合わせる"""
    context = DsqlContext(region=REGION, tag_name=TAG_NAME, deletion_protection=False)
    dsql = Dsql(context)
    monkeypatch.setattr(Dsql, "_ensure_backup_role", lambda self: ROLE_ARN)
    rp_arn = "arn:aws:backup:us-east-1:123456789012:recovery-point:rp-1"
    stubber = Stubber(dsql._backup)
    stubber.add_response(
        "start_restore_job",
        {"RestoreJobId": "restore-1"},
        {
            "RecoveryPointArn": rp_arn,
            "IamRoleArn": ROLE_ARN,
            "Metadata": {
                "regionalConfig": json.dumps(
                    [{"region": REGION, "isDeletionProtectionEnabled": False}]
                )
            },
        },
    )
    with stubber:
        assert dsql.start_restore(rp_arn) == "restore-1"
    stubber.assert_no_pending_responses()


def test_latest_recovery_point_ignores_incomplete():
    """未完了の recovery point は復元候補にしない"""
    dsql, _ = _make_dsql()
    dsql.__dict__["cluster"] = _get_cluster_response("abc123")
    stubber = Stubber(dsql._backup)
    stubber.add_response(
        "list_recovery_points_by_resource",
        {
            "RecoveryPoints": [
                {
                    "RecoveryPointArn": "arn:aws:backup:us-east-1:1:recovery-point:old",
                    "Status": "COMPLETED",
                    "CreationDate": datetime(2026, 8, 1),
                },
                {
                    "RecoveryPointArn": "arn:aws:backup:us-east-1:1:recovery-point:new",
                    "Status": "PARTIAL",
                    "CreationDate": datetime(2026, 8, 3),
                },
            ]
        },
        {"ResourceArn": ARN},
    )
    with stubber:
        point = dsql.latest_recovery_point()
    assert point is not None
    assert point["RecoveryPointArn"].endswith(":old")


def test_restore_cli_switches_and_warns(monkeypatch):
    """復元完了後に切り替え、deploy 未実施と旧クラスター課金を警告する"""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    dsql, dsql_stub = _make_dsql()
    dsql.__dict__["cluster"] = _get_cluster_response("old123")
    new_arn = "arn:aws:dsql:us-east-1:123456789012:cluster/new456"
    rp_arn = "arn:aws:backup:us-east-1:123456789012:recovery-point:rp-1"
    monkeypatch.setattr(Dsql, "_ensure_backup_role", lambda self: ROLE_ARN)
    monkeypatch.setattr(Dsql, "publish_endpoint", lambda self: None)

    backup_stub = Stubber(dsql._backup)
    backup_stub.add_response("start_restore_job", {"RestoreJobId": "restore-1"})
    backup_stub.add_response(
        "describe_restore_job",
        {
            "RestoreJobId": "restore-1",
            "Status": "COMPLETED",
            "CreatedResourceArn": new_arn,
        },
        {"RestoreJobId": "restore-1"},
    )
    dsql_stub.add_response(
        "tag_resource",
        {},
        {"resourceArn": ARN, "tags": {"Name": "%s-replaced-old123" % TAG_NAME}},
    )
    dsql_stub.add_response(
        "tag_resource", {}, {"resourceArn": new_arn, "tags": {"Name": TAG_NAME}}
    )
    # 切り替え後の cluster 解決 (新クラスターを返す)
    dsql_stub.add_response(
        "list_clusters", {"clusters": [{"identifier": "new456", "arn": new_arn}]}
    )
    dsql_stub.add_response(
        "get_cluster",
        {
            "identifier": "new456",
            "arn": new_arn,
            "status": "ACTIVE",
            "creationTime": CREATED,
            "deletionProtectionEnabled": False,
        },
        {"identifier": "new456"},
    )
    dsql_stub.add_response(
        "list_tags_for_resource", {"tags": {"Name": TAG_NAME}}, {"resourceArn": new_arn}
    )
    monkeypatch.setattr(dsql_cli, "_get_dsql_resource", lambda stage: dsql)
    runner = CliRunner()
    with backup_stub, dsql_stub:
        result = runner.invoke(
            dsql_cli.dsql,
            ["restore", "--stage", "dev", rp_arn, "--skip-backup"],
        )
    assert result.exit_code == 0, result.output
    err = result.stderr.replace("\n", "")
    assert "pocket deploy" in err
    assert "旧クラスター" in err and "課金" in err
    backup_stub.assert_no_pending_responses()
    dsql_stub.assert_no_pending_responses()


def test_restore_cli_requires_source():
    """recovery point 未指定かつ --latest なしはエラー"""
    dsql, _ = _make_dsql()
    runner = CliRunner()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(dsql_cli, "_get_dsql_resource", lambda stage: dsql)
        result = runner.invoke(dsql_cli.dsql, ["restore", "--stage", "dev"])
    assert result.exit_code == 1
    assert "--latest" in result.stderr
