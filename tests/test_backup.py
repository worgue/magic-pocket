"""[backup] (DB 層の定期バックアップ = AWS Backup plan) のテスト。

エンジン別宣言 ([backup.dsql] / [backup.rds]) の GFS 既定と厳密検証
(対象資源の必須化・rds への cold storage 禁止等)、Backup resource の
エンジン別 plan 管理 (冪等性・権限不足時の warn-and-continue)、recovery
point の削除経路 (cleanup CLI) を固定する。AWS Backup の service model は
PascalCase のため Stubber で casing も検証する。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from botocore.stub import Stubber
from click.testing import CliRunner
from pocket_cli.cli import backup_cli
from pocket_cli.resources.backup import Backup
from pydantic import ValidationError

from pocket.context import BackupContext, BackupPlanContext, BackupRuleContext, Context
from pocket.settings import Backup as BackupSettings
from pocket.settings import Settings

REGION = "us-east-1"
DSQL_PLAN = "dev-testprj-pocket-backup-dsql"
RDS_PLAN = "dev-testprj-pocket-backup-rds"
LEGACY_DSQL_PLAN = "dev-testprj-pocket-dsql-backup"  # 0.27 以前の旧形式名
DSQL_ARN = "arn:aws:dsql:us-east-1:123456789012:cluster/abc123"
RDS_ARN = "arn:aws:rds:us-east-1:123456789012:cluster:dev-testprj-pocket-rds"
ROLE_ARN = "arn:aws:iam::123456789012:role/forge-pocket-backup-role"
RP_ARN = "arn:aws:backup:us-east-1:123456789012:recovery-point:rp-%d"

RDS_UNMANAGED = {
    "managed": False,
    "secret_arn": "arn:aws:secretsmanager:us-east-1:1:secret:x",
    "security_group_id": "sg-123",
}


def _dsql_plan_context() -> BackupPlanContext:
    return BackupPlanContext(
        service="dsql",
        plan_name=DSQL_PLAN,
        rules=[
            BackupRuleContext(
                name="daily",
                schedule_expression="cron(0 3 * * ? *)",
                delete_after_days=35,
            ),
            BackupRuleContext(
                name="monthly",
                schedule_expression="cron(0 5 1 * ? *)",
                delete_after_days=365,
                cold_storage_after_days=90,
            ),
        ],
    )


def _rds_plan_context() -> BackupPlanContext:
    return BackupPlanContext(
        service="rds",
        plan_name=RDS_PLAN,
        rules=[
            BackupRuleContext(
                name="weekly",
                schedule_expression="cron(0 4 ? * 1 *)",
                delete_after_days=365,
            ),
        ],
    )


def _make_backup(
    plans: list[BackupPlanContext] | None = None,
    cleanup_plan_names: list[str] | None = None,
    **overrides,
) -> tuple[Backup, Stubber]:
    data: dict = {
        "region": REGION,
        "deletable": False,
        "timezone": "Asia/Tokyo",
        "plans": [_dsql_plan_context()] if plans is None else plans,
        "cleanup_plan_names": (
            [DSQL_PLAN] if cleanup_plan_names is None else cleanup_plan_names
        ),
    }
    data.update(overrides)
    backup = Backup(BackupContext(**data))
    return backup, Stubber(backup._backup)


def _set_targets(monkeypatch, arns: dict[str, str | None]):
    monkeypatch.setattr(
        Backup, "_service_target_arn", lambda self, service: arns.get(service)
    )


def _ensure_role(monkeypatch):
    monkeypatch.setattr(
        "pocket_cli.resources.backup.ensure_backup_role",
        lambda iam_client, boundary: ROLE_ARN,
    )


def _build_settings(**overrides) -> Settings:
    data: dict = {
        "stage": "dev",
        "general": {
            "region": REGION,
            "project_name": "testprj",
            "stages": ["dev", "prod"],
        },
    }
    data.update(overrides)
    return Settings.model_validate(data)


def test_backup_settings_gfs_tier_defaults():
    """GFS 階層の既定: dsql = daily 35 / weekly 365 / monthly 1095、
    rds = weekly 365 / monthly 1095 (daily は PITR が担うため無い)。

    dsql の長期階層 (weekly / monthly) は 90 日で cold storage へ移す。
    monthly 1095 日はオンデマンドバックアップの既定と同じ (保持ポリシーが
    食い違わない)。
    """
    backup = BackupSettings.model_validate({"dsql": {}, "rds": {}})
    assert backup.deletable is False
    assert backup.timezone == "UTC"
    assert backup.dsql is not None and backup.rds is not None
    # dsql: 3 階層
    assert backup.dsql.daily.cron == "0 3 * * ? *"
    assert backup.dsql.daily.delete_after_days == 35
    assert backup.dsql.daily.cold_storage_after_days == 0
    assert backup.dsql.weekly.cron == "0 4 ? * 1 *"
    assert backup.dsql.weekly.delete_after_days == 365
    assert backup.dsql.weekly.cold_storage_after_days == 90
    assert backup.dsql.monthly.cron == "0 5 1 * ? *"
    assert backup.dsql.monthly.delete_after_days == 1095
    assert backup.dsql.monthly.cold_storage_after_days == 90
    # rds: 2 階層
    assert backup.rds.weekly.delete_after_days == 365
    assert backup.rds.monthly.delete_after_days == 1095


def test_backup_settings_rejects_cold_storage_shorter_than_90_days():
    """AWS Backup の cold storage 最低保持期間 (90 日) を settings で弾く"""
    with pytest.raises(ValidationError, match="cold_storage_after_days"):
        BackupSettings.model_validate(
            {
                "dsql": {
                    "daily": {
                        "cold_storage_after_days": 35,
                        "delete_after_days": 100,
                    }
                }
            }
        )
    # 境界 (35 + 90) は許容される
    backup = BackupSettings.model_validate(
        {"dsql": {"daily": {"cold_storage_after_days": 35, "delete_after_days": 125}}}
    )
    assert backup.dsql is not None
    assert backup.dsql.daily.delete_after_days == 125


def test_backup_settings_strict_rejections():
    """backup 関連は厳密に弾く (silent skip や無視をしない)。

    - [backup] 単体 (エンジン宣言なし)
    - [backup.neon] などスキーマに無いエンジン
    - [backup.rds] への cold_storage_after_days (Aurora は cold storage 非対応)
    - [backup.rds.daily] (daily は PITR の責務)
    """
    with pytest.raises(ValidationError, match="backup.dsql"):
        BackupSettings.model_validate({})
    with pytest.raises(ValidationError, match="neon"):
        BackupSettings.model_validate({"neon": {}})
    with pytest.raises(ValidationError, match="cold_storage_after_days"):
        BackupSettings.model_validate(
            {"rds": {"weekly": {"cold_storage_after_days": 90}}}
        )
    with pytest.raises(ValidationError, match="daily"):
        BackupSettings.model_validate({"rds": {"daily": {}}})


def test_backup_settings_requires_target_resources():
    """宣言したエンジンの資源が stage に無ければエラー (fail-loud)。

    「書いたのに守られていない」を防ぐため、warning ではなく検証で止める。
    """
    with pytest.raises(ValidationError, match=r"backup.dsql.*dsql"):
        _build_settings(backup={"dsql": {}})
    with pytest.raises(ValidationError, match=r"backup.rds.*rds"):
        _build_settings(dsql={}, backup={"rds": {}})
    with pytest.raises(ValidationError, match="managed"):
        _build_settings(rds=RDS_UNMANAGED, backup={"rds": {}})


def test_context_builds_plan_per_engine():
    """宣言されたエンジンごとに plan context が立つ。

    dsql 側 context には dsql plan の参照が渡り、オンデマンド backup の
    保持継承 (最長 = monthly) と deploy_init の警告スキップに使われる。
    """
    rds_kwargs: dict = {
        "vpc": {"ref": "main", "zone_suffixes": ["a", "c"]},
        "awscontainer": {
            "dockerfile_path": "Dockerfile",
            "handlers": {},
            "vpc": {"ref": "main", "zone_suffixes": ["a", "c"]},
        },
    }
    context = Context.from_settings(
        _build_settings(
            dsql={},
            rds={"vpc": {"ref": "main", "zone_suffixes": ["a", "c"]}},
            backup={"dsql": {}, "rds": {}},
            **rds_kwargs,
        )
    )
    assert context.backup is not None
    assert context.backup.declared is True
    assert [p.plan_name for p in context.backup.plans] == [DSQL_PLAN, RDS_PLAN]
    dsql_plan = context.backup.plan_for("dsql")
    assert dsql_plan is not None
    assert [r.name for r in dsql_plan.rules] == ["daily", "weekly", "monthly"]
    assert dsql_plan.rules[0].schedule_expression == "cron(0 3 * * ? *)"
    assert dsql_plan.rules[2].cold_storage_after_days == 90
    assert dsql_plan.monthly_delete_after_days == 1095
    rds_plan = context.backup.plan_for("rds")
    assert rds_plan is not None
    assert [r.name for r in rds_plan.rules] == ["weekly", "monthly"]
    assert all(r.cold_storage_after_days == 0 for r in rds_plan.rules)
    assert context.dsql is not None
    assert context.dsql.backup is not None
    assert context.dsql.backup.monthly_delete_after_days == 1095


def test_context_undeclared_is_optin():
    """[backup] 未宣言なら plans は空 (deploy は plan を作らない)。

    context 自体は立つ (destroy が plan の掃除と recovery point の案内を担う
    ため)。cleanup_plan_names は宣言でなく資源の有無から導出する。
    dsql 側の参照は None になり、deploy_init が無防備警告を出す。
    """
    context = Context.from_settings(_build_settings(dsql={}))
    assert context.backup is not None
    assert context.backup.declared is False
    assert context.backup.plans == []
    # 旧形式名 (0.27 以前) も destroy の掃除対象に含める
    assert context.backup.cleanup_plan_names == [DSQL_PLAN, LEGACY_DSQL_PLAN]
    assert context.backup.legacy_plan_names == [LEGACY_DSQL_PLAN]
    assert context.dsql is not None
    assert context.dsql.backup is None


def test_context_none_without_db_target():
    """対象 DB (dsql / managed rds) が無い stage では context を作らない"""
    assert Context.from_settings(_build_settings()).backup is None
    assert Context.from_settings(_build_settings(rds=RDS_UNMANAGED)).backup is None


def _stub_plan_create(stubber, plan_name: str, plan_id: str, rules: list[dict]):
    stubber.add_response("list_backup_plans", {"BackupPlansList": []})
    stubber.add_response(
        "create_backup_plan",
        {"BackupPlanId": plan_id},
        {"BackupPlan": {"BackupPlanName": plan_name, "Rules": rules}},
    )


def _stub_selection_create(stubber, plan_name: str, plan_id: str, arns: list[str]):
    stubber.add_response(
        "list_backup_selections",
        {"BackupSelectionsList": []},
        {"BackupPlanId": plan_id},
    )
    stubber.add_response(
        "create_backup_selection",
        {"SelectionId": "sel-%s" % plan_id},
        {
            "BackupPlanId": plan_id,
            "BackupSelection": {
                "SelectionName": plan_name,
                "IamRoleArn": ROLE_ARN,
                "Resources": arns,
            },
        },
    )


def test_ensure_creates_plan_and_selection_per_engine(monkeypatch):
    """エンジンごとに plan を作り、それぞれの cluster を selection する。

    rule 構成がエンジンごとに違う (rds は daily 無し / cold storage 無し) ため
    plan を分ける必要がある (plan の rule は selection 全体に一律適用のため)。
    timezone は共通設定が全 rule に渡る。
    """
    _ensure_role(monkeypatch)
    _set_targets(monkeypatch, {"dsql": DSQL_ARN, "rds": RDS_ARN})
    backup, stubber = _make_backup(
        plans=[_dsql_plan_context(), _rds_plan_context()],
        cleanup_plan_names=[DSQL_PLAN, RDS_PLAN],
    )
    stubber.add_client_error(
        "create_backup_vault",
        service_error_code="AlreadyExistsException",
        expected_params={"BackupVaultName": "pocket-backup"},
    )
    _stub_plan_create(
        stubber,
        DSQL_PLAN,
        "plan-dsql",
        [
            {
                "RuleName": "daily",
                "TargetBackupVaultName": "pocket-backup",
                "ScheduleExpression": "cron(0 3 * * ? *)",
                "ScheduleExpressionTimezone": "Asia/Tokyo",
                "Lifecycle": {"DeleteAfterDays": 35},
            },
            {
                "RuleName": "monthly",
                "TargetBackupVaultName": "pocket-backup",
                "ScheduleExpression": "cron(0 5 1 * ? *)",
                "ScheduleExpressionTimezone": "Asia/Tokyo",
                "Lifecycle": {
                    "DeleteAfterDays": 365,
                    "MoveToColdStorageAfterDays": 90,
                },
            },
        ],
    )
    _stub_selection_create(stubber, DSQL_PLAN, "plan-dsql", [DSQL_ARN])
    _stub_plan_create(
        stubber,
        RDS_PLAN,
        "plan-rds",
        [
            {
                "RuleName": "weekly",
                "TargetBackupVaultName": "pocket-backup",
                "ScheduleExpression": "cron(0 4 ? * 1 *)",
                "ScheduleExpressionTimezone": "Asia/Tokyo",
                "Lifecycle": {"DeleteAfterDays": 365},
            },
        ],
    )
    _stub_selection_create(stubber, RDS_PLAN, "plan-rds", [RDS_ARN])
    with stubber:
        backup.ensure_post_deploy_state()
    stubber.assert_no_pending_responses()


def test_ensure_is_idempotent_regardless_of_rule_order(monkeypatch):
    """既存 plan が宣言どおりなら update しない (rule の返却順にも依存しない)。

    GetBackupPlan の rule 順序は保証されないため RuleName で突き合わせる。
    AWS が補完する既定値 (StartWindowMinutes 等) も drift 判定に含めない。
    """
    _ensure_role(monkeypatch)
    _set_targets(monkeypatch, {"dsql": DSQL_ARN})
    backup, stubber = _make_backup()
    stubber.add_client_error(
        "create_backup_vault",
        service_error_code="AlreadyExistsException",
        expected_params={"BackupVaultName": "pocket-backup"},
    )
    stubber.add_response(
        "list_backup_plans",
        {"BackupPlansList": [{"BackupPlanId": "plan-1", "BackupPlanName": DSQL_PLAN}]},
    )
    stubber.add_response(
        "get_backup_plan",
        {
            "BackupPlan": {
                "BackupPlanName": DSQL_PLAN,
                "Rules": [
                    # 宣言と逆順 + AWS 補完項目つき
                    {
                        "RuleName": "monthly",
                        "RuleId": "generated-by-aws",
                        "TargetBackupVaultName": "pocket-backup",
                        "ScheduleExpression": "cron(0 5 1 * ? *)",
                        "ScheduleExpressionTimezone": "Asia/Tokyo",
                        "StartWindowMinutes": 60,
                        "Lifecycle": {
                            "DeleteAfterDays": 365,
                            "MoveToColdStorageAfterDays": 90,
                            "OptInToArchiveForSupportedResources": False,
                        },
                    },
                    {
                        "RuleName": "daily",
                        "RuleId": "generated-by-aws-2",
                        "TargetBackupVaultName": "pocket-backup",
                        "ScheduleExpression": "cron(0 3 * * ? *)",
                        "ScheduleExpressionTimezone": "Asia/Tokyo",
                        "CompletionWindowMinutes": 180,
                        "Lifecycle": {"DeleteAfterDays": 35},
                    },
                ],
            }
        },
        {"BackupPlanId": "plan-1"},
    )
    stubber.add_response(
        "list_backup_selections",
        {
            "BackupSelectionsList": [
                {"SelectionId": "sel-1", "SelectionName": DSQL_PLAN}
            ]
        },
        {"BackupPlanId": "plan-1"},
    )
    stubber.add_response(
        "get_backup_selection",
        {
            "BackupSelection": {
                "SelectionName": DSQL_PLAN,
                "IamRoleArn": ROLE_ARN,
                "Resources": [DSQL_ARN],
            }
        },
        {"BackupPlanId": "plan-1", "SelectionId": "sel-1"},
    )
    with stubber:
        backup.ensure_post_deploy_state()
    stubber.assert_no_pending_responses()


def test_ensure_recreates_selection_when_target_changes(monkeypatch):
    """対象 ARN が変わったら selection を作り直す (更新 API が無いため)。

    cluster 再作成 (restore 切替等) で起きる。
    """
    _ensure_role(monkeypatch)
    new_arn = "arn:aws:dsql:us-east-1:123456789012:cluster/new456"
    _set_targets(monkeypatch, {"dsql": new_arn})
    backup, stubber = _make_backup()
    stubber.add_client_error(
        "create_backup_vault",
        service_error_code="AlreadyExistsException",
        expected_params={"BackupVaultName": "pocket-backup"},
    )
    stubber.add_response(
        "list_backup_plans",
        {"BackupPlansList": [{"BackupPlanId": "plan-1", "BackupPlanName": DSQL_PLAN}]},
    )
    stubber.add_response(
        "get_backup_plan",
        {
            "BackupPlan": {
                "BackupPlanName": DSQL_PLAN,
                "Rules": [
                    {
                        "RuleName": "daily",
                        "TargetBackupVaultName": "pocket-backup",
                        "ScheduleExpression": "cron(0 3 * * ? *)",
                        "ScheduleExpressionTimezone": "Asia/Tokyo",
                        "Lifecycle": {"DeleteAfterDays": 35},
                    },
                    {
                        "RuleName": "monthly",
                        "TargetBackupVaultName": "pocket-backup",
                        "ScheduleExpression": "cron(0 5 1 * ? *)",
                        "ScheduleExpressionTimezone": "Asia/Tokyo",
                        "Lifecycle": {
                            "DeleteAfterDays": 365,
                            "MoveToColdStorageAfterDays": 90,
                        },
                    },
                ],
            }
        },
        {"BackupPlanId": "plan-1"},
    )
    stubber.add_response(
        "list_backup_selections",
        {
            "BackupSelectionsList": [
                {"SelectionId": "sel-1", "SelectionName": DSQL_PLAN}
            ]
        },
        {"BackupPlanId": "plan-1"},
    )
    stubber.add_response(
        "get_backup_selection",
        {
            "BackupSelection": {
                "SelectionName": DSQL_PLAN,
                "IamRoleArn": ROLE_ARN,
                "Resources": [DSQL_ARN],  # 旧 cluster
            }
        },
        {"BackupPlanId": "plan-1", "SelectionId": "sel-1"},
    )
    stubber.add_response(
        "delete_backup_selection",
        {},
        {"BackupPlanId": "plan-1", "SelectionId": "sel-1"},
    )
    stubber.add_response(
        "create_backup_selection",
        {"SelectionId": "sel-2"},
        {
            "BackupPlanId": "plan-1",
            "BackupSelection": {
                "SelectionName": DSQL_PLAN,
                "IamRoleArn": ROLE_ARN,
                "Resources": [new_arn],
            },
        },
    )
    with stubber:
        backup.ensure_post_deploy_state()
    stubber.assert_no_pending_responses()


def test_ensure_undeclared_touches_nothing(monkeypatch):
    """[backup] 未宣言 (opt-in) なら API を一切呼ばない。

    宣言を外した後の既存 plan にも触らない (掃除は destroy の責務)。
    """
    _set_targets(monkeypatch, {"dsql": DSQL_ARN})
    backup, stubber = _make_backup(plans=[])
    with stubber:  # 応答を登録しない = 呼んだら例外になる
        backup.ensure_post_deploy_state()
    stubber.assert_no_pending_responses()


def test_ensure_deletes_legacy_plan_before_provisioning(monkeypatch):
    """0.27 以前の旧形式名 ({prefix}dsql-backup) の plan は deploy で削除する。

    0.28.0 の改名 (dsql-backup → backup-dsql) で旧 plan が孤児として残り、
    新 plan と同じ cluster に二重でバックアップが走るため (KN1041)。
    selection → plan の順に消してから通常の provisioning を行う。
    """
    _ensure_role(monkeypatch)
    _set_targets(monkeypatch, {"dsql": DSQL_ARN})
    backup, stubber = _make_backup(legacy_plan_names=[LEGACY_DSQL_PLAN])
    # legacy 掃除: 検出 → selection 削除 → plan 削除
    stubber.add_response(
        "list_backup_plans",
        {
            "BackupPlansList": [
                {"BackupPlanId": "plan-legacy", "BackupPlanName": LEGACY_DSQL_PLAN}
            ]
        },
    )
    stubber.add_response(
        "list_backup_selections",
        {
            "BackupSelectionsList": [
                {"SelectionId": "sel-legacy", "SelectionName": LEGACY_DSQL_PLAN}
            ]
        },
        {"BackupPlanId": "plan-legacy"},
    )
    stubber.add_response(
        "delete_backup_selection",
        {},
        {"BackupPlanId": "plan-legacy", "SelectionId": "sel-legacy"},
    )
    stubber.add_response("delete_backup_plan", {}, {"BackupPlanId": "plan-legacy"})
    # 以降は通常の provisioning
    stubber.add_client_error(
        "create_backup_vault",
        service_error_code="AlreadyExistsException",
        expected_params={"BackupVaultName": "pocket-backup"},
    )
    _stub_plan_create(
        stubber,
        DSQL_PLAN,
        "plan-dsql",
        [
            {
                "RuleName": "daily",
                "TargetBackupVaultName": "pocket-backup",
                "ScheduleExpression": "cron(0 3 * * ? *)",
                "ScheduleExpressionTimezone": "Asia/Tokyo",
                "Lifecycle": {"DeleteAfterDays": 35},
            },
            {
                "RuleName": "monthly",
                "TargetBackupVaultName": "pocket-backup",
                "ScheduleExpression": "cron(0 5 1 * ? *)",
                "ScheduleExpressionTimezone": "Asia/Tokyo",
                "Lifecycle": {
                    "DeleteAfterDays": 365,
                    "MoveToColdStorageAfterDays": 90,
                },
            },
        ],
    )
    _stub_selection_create(stubber, DSQL_PLAN, "plan-dsql", [DSQL_ARN])
    with stubber:
        backup.ensure_post_deploy_state()
    stubber.assert_no_pending_responses()


def test_ensure_deletes_legacy_plan_even_when_undeclared(monkeypatch):
    """[backup] 未宣言でも旧形式名の plan は deploy で削除する。

    旧スキーマ ([dsql.backup]) から宣言を外して 0.28.0 に上げた場合も、
    孤児 plan が silent に走り続けるのを防ぐ。新 plan の provisioning は
    行わない (opt-in のまま)。
    """
    _set_targets(monkeypatch, {"dsql": DSQL_ARN})
    backup, stubber = _make_backup(plans=[], legacy_plan_names=[LEGACY_DSQL_PLAN])
    stubber.add_response(
        "list_backup_plans",
        {
            "BackupPlansList": [
                {"BackupPlanId": "plan-legacy", "BackupPlanName": LEGACY_DSQL_PLAN}
            ]
        },
    )
    stubber.add_response(
        "list_backup_selections",
        {"BackupSelectionsList": []},
        {"BackupPlanId": "plan-legacy"},
    )
    stubber.add_response("delete_backup_plan", {}, {"BackupPlanId": "plan-legacy"})
    with stubber:
        backup.ensure_post_deploy_state()
    # vault / role / 新 plan の API は登録していない = 呼ばれていない
    stubber.assert_no_pending_responses()


def test_ensure_legacy_cleanup_access_denied_warns_and_continues(monkeypatch, capsys):
    """legacy 掃除に権限が無くても deploy を落とさない (警告のみ)"""
    _set_targets(monkeypatch, {"dsql": DSQL_ARN})
    backup, stubber = _make_backup(plans=[], legacy_plan_names=[LEGACY_DSQL_PLAN])
    stubber.add_client_error(
        "list_backup_plans",
        service_error_code="AccessDeniedException",
    )
    with stubber:
        backup.ensure_post_deploy_state()  # raise しない
    err = capsys.readouterr().err.replace("\n", "")
    assert "権限" in err
    assert LEGACY_DSQL_PLAN in err


def test_ensure_noop_without_created_targets(monkeypatch):
    """対象 cluster が未作成 (ARN 無し) なら API を一切呼ばない。

    selection の無い空 plan を作らない (cluster 作成後の deploy で作られる)。
    """
    _set_targets(monkeypatch, {})
    backup, stubber = _make_backup()
    with stubber:
        backup.ensure_post_deploy_state()
    stubber.assert_no_pending_responses()


def test_ensure_access_denied_warns_and_continues(monkeypatch, capsys):
    """backup 権限の無い deploy role では警告して deploy を続行する。

    [backup] 宣言だけ先行して権限付与が追いついていない場合に、deploy 全体を
    落とさない (対処 = backup:* の付与を案内)。
    """
    _set_targets(monkeypatch, {"dsql": DSQL_ARN})
    backup, stubber = _make_backup()
    stubber.add_client_error(
        "create_backup_vault",
        service_error_code="AccessDeniedException",
        expected_params={"BackupVaultName": "pocket-backup"},
    )
    with stubber:
        backup.ensure_post_deploy_state()  # raise しない
    err = capsys.readouterr().err.replace("\n", "")
    assert "権限" in err
    assert "backup:*" in err


def test_delete_removes_all_stage_plans_but_not_recovery_points():
    """destroy は stage の全 plan/selection を消すが vault/recovery point は
    消さない。宣言の有無に関わらず cleanup_plan_names を走査する。"""
    backup, stubber = _make_backup(plans=[], cleanup_plan_names=[DSQL_PLAN, RDS_PLAN])
    for plan_name, plan_id in ((DSQL_PLAN, "plan-1"), (RDS_PLAN, "plan-2")):
        stubber.add_response(
            "list_backup_plans",
            {
                "BackupPlansList": [
                    {"BackupPlanId": plan_id, "BackupPlanName": plan_name}
                ]
            },
        )
        stubber.add_response(
            "list_backup_selections",
            {
                "BackupSelectionsList": [
                    {"SelectionId": "sel-%s" % plan_id, "SelectionName": plan_name}
                ]
            },
            {"BackupPlanId": plan_id},
        )
        stubber.add_response(
            "delete_backup_selection",
            {},
            {"BackupPlanId": plan_id, "SelectionId": "sel-%s" % plan_id},
        )
        stubber.add_response("delete_backup_plan", {}, {"BackupPlanId": plan_id})
    with stubber:
        backup.delete()
    # vault / recovery point の削除 API は登録していない = 呼ばれていない
    stubber.assert_no_pending_responses()


def test_delete_access_denied_warns_and_continues(capsys):
    """backup 権限を持たない deploy role でも destroy 自体は完遂させる"""
    backup, stubber = _make_backup()
    stubber.add_client_error(
        "list_backup_plans",
        service_error_code="AccessDeniedException",
    )
    with stubber:
        backup.delete()  # raise しない
    err = capsys.readouterr().err.replace("\n", "")
    assert "権限" in err


def test_list_recovery_points_filters_to_pocket_vault(monkeypatch):
    """pocket-backup vault 内の recovery point だけを対象にする。

    `--vault=my-vault` で利用者の vault に取ったオンデマンド分は利用者の
    管理物とみなし、pocket からは消さない。
    """
    monkeypatch.setattr(Backup, "target_arns", lambda self: [DSQL_ARN])
    backup, stubber = _make_backup()
    stubber.add_response(
        "list_recovery_points_by_resource",
        {
            "RecoveryPoints": [
                {
                    "RecoveryPointArn": RP_ARN % 1,
                    "BackupVaultName": "pocket-backup",
                    "CreationDate": datetime(2026, 8, 1),
                },
                {
                    "RecoveryPointArn": RP_ARN % 2,
                    "BackupVaultName": "my-vault",
                    "CreationDate": datetime(2026, 8, 2),
                },
            ]
        },
        {"ResourceArn": DSQL_ARN},
    )
    with stubber:
        points = backup.list_recovery_points()
    assert points is not None
    assert [p["RecoveryPointArn"] for p in points] == [RP_ARN % 1]


def test_list_recovery_points_access_denied_returns_none(monkeypatch):
    """権限が無ければ None (空リストとは区別して案内をスキップできる)"""
    monkeypatch.setattr(Backup, "target_arns", lambda self: [DSQL_ARN])
    backup, stubber = _make_backup()
    stubber.add_client_error(
        "list_recovery_points_by_resource",
        service_error_code="AccessDeniedException",
    )
    with stubber:
        assert backup.list_recovery_points() is None


def test_delete_recovery_points_deletes_each():
    """recovery point を vault 名 + ARN で 1 件ずつ削除する"""
    backup, stubber = _make_backup(deletable=True)
    for i in (1, 2):
        stubber.add_response(
            "delete_recovery_point",
            {},
            {
                "BackupVaultName": "pocket-backup",
                "RecoveryPointArn": RP_ARN % i,
            },
        )
    points = [
        {"BackupVaultName": "pocket-backup", "RecoveryPointArn": RP_ARN % i}
        for i in (1, 2)
    ]
    with stubber:
        assert backup.delete_recovery_points(points) == 2
    stubber.assert_no_pending_responses()


def _cleanup_context(deletable: bool, declared: bool = True) -> Context:
    data: dict = {"dsql": {}}
    if declared:
        data["backup"] = {"dsql": {}, "deletable": deletable}
    return Context.from_settings(_build_settings(**data))


def test_cleanup_cli_requires_deletable_declaration(monkeypatch):
    """`pocket backup cleanup` は [backup] deletable = true が無ければ拒否する。

    誤操作でバックアップデータを失わないためのガード。エラーメッセージで
    宣言方法を案内する。
    """
    monkeypatch.setattr(
        Context, "from_toml", classmethod(lambda cls, stage: _cleanup_context(False))
    )
    result = CliRunner().invoke(backup_cli.backup, ["cleanup", "--stage", "dev"])
    assert result.exit_code != 0
    assert "deletable = true" in result.output

    monkeypatch.setattr(
        Context,
        "from_toml",
        classmethod(lambda cls, stage: _cleanup_context(False, declared=False)),
    )
    result = CliRunner().invoke(backup_cli.backup, ["cleanup", "--stage", "dev"])
    assert result.exit_code != 0
    assert "宣言" in result.output


def test_cleanup_cli_deletes_after_confirmation(monkeypatch):
    """deletable = true なら一覧表示 → 確認 → 削除まで通る"""
    monkeypatch.setattr(
        Context, "from_toml", classmethod(lambda cls, stage: _cleanup_context(True))
    )
    points = [
        {
            "BackupVaultName": "pocket-backup",
            "RecoveryPointArn": RP_ARN % 1,
            "CreationDate": datetime(2026, 8, 1),
        }
    ]
    monkeypatch.setattr(Backup, "list_recovery_points", lambda self: points)
    deleted: list = []
    monkeypatch.setattr(
        Backup,
        "delete_recovery_points",
        lambda self, pts: deleted.extend(pts) or len(pts),
    )
    result = CliRunner().invoke(
        backup_cli.backup, ["cleanup", "--stage", "dev", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert deleted == points


def test_cleanup_cli_noop_without_points(monkeypatch):
    """recovery point が無ければ何も消さずその旨を表示する"""
    monkeypatch.setattr(
        Context, "from_toml", classmethod(lambda cls, stage: _cleanup_context(True))
    )
    monkeypatch.setattr(Backup, "list_recovery_points", lambda self: [])
    result = CliRunner().invoke(backup_cli.backup, ["cleanup", "--stage", "dev"])
    assert result.exit_code == 0, result.output
    assert "ありません" in result.stderr
