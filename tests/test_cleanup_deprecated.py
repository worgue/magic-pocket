"""pocket cleanup-deprecated: 0.36 以前の account 共有 backup リソースの掃除。"""

from datetime import datetime
from unittest import mock

import pytest
from botocore.stub import Stubber
from click.testing import CliRunner
from pocket_cli.cli import interaction
from pocket_cli.cli.cleanup_deprecated_cli import cleanup_deprecated
from pocket_cli.resources.aws.backup_legacy import LegacyBackupCleanup

REGION = "us-east-1"
VAULT = "pocket-backup"
ROLE = "forge-pocket-backup-role"
ROLE_ARN = "arn:aws:iam::123456789012:role/%s" % ROLE
NEW_ROLE_ARN = "arn:aws:iam::123456789012:role/dev-testprj-pocket-backup-role"
RP_ARN = "arn:aws:backup:us-east-1:123456789012:recovery-point:rp-%d"


@pytest.fixture(autouse=True)
def _reset_assume_yes(monkeypatch):
    monkeypatch.setattr(interaction, "_assume_yes", False)


def _stub_plan(stubber: Stubber, vault: str, role_arn: str) -> None:
    stubber.add_response(
        "list_backup_plans",
        {"BackupPlansList": [{"BackupPlanId": "plan-1", "BackupPlanName": "p"}]},
    )
    stubber.add_response(
        "get_backup_plan",
        {
            "BackupPlan": {
                "BackupPlanName": "p",
                "Rules": [{"RuleName": "daily", "TargetBackupVaultName": vault}],
            }
        },
        {"BackupPlanId": "plan-1"},
    )
    stubber.add_response(
        "list_backup_selections",
        {"BackupSelectionsList": [{"SelectionId": "sel-1", "SelectionName": "p"}]},
        {"BackupPlanId": "plan-1"},
    )
    stubber.add_response(
        "get_backup_selection",
        {
            "BackupSelection": {
                "SelectionName": "p",
                "IamRoleArn": role_arn,
                "Resources": [],
            }
        },
        {"BackupPlanId": "plan-1", "SelectionId": "sel-1"},
    )


def test_references_detects_plans_and_selections_not_yet_migrated():
    """未更新の stage が残っていれば、旧 vault / 旧ロールの参照として検出する。"""
    cleanup = LegacyBackupCleanup(REGION)
    stubber = Stubber(cleanup._backup)
    _stub_plan(stubber, VAULT, ROLE_ARN)
    with stubber:
        refs = cleanup.references()
    assert len(refs) == 2
    assert VAULT in refs[0]
    assert ROLE in refs[1]


def test_references_empty_after_all_stages_migrated():
    cleanup = LegacyBackupCleanup(REGION)
    stubber = Stubber(cleanup._backup)
    _stub_plan(stubber, "dev-testprj-pocket-backup", NEW_ROLE_ARN)
    with stubber:
        assert cleanup.references() == []


def test_last_expiry():
    points = [
        {"CalculatedLifecycle": {"DeleteAt": datetime(2026, 10, 1)}},
        {"CalculatedLifecycle": {"DeleteAt": datetime(2027, 1, 1)}},
    ]
    assert LegacyBackupCleanup.last_expiry(points) == datetime(2027, 1, 1)
    # 無期限保持が混じると自動では空にならない
    assert LegacyBackupCleanup.last_expiry([*points, {}]) is None


def test_delete_vault_reports_not_yet_empty():
    """recovery point の削除が未反映だと AWS は InvalidRequestException を返す。"""
    cleanup = LegacyBackupCleanup(REGION)
    stubber = Stubber(cleanup._backup)
    stubber.add_client_error(
        "delete_backup_vault",
        service_error_code="InvalidRequestException",
        expected_params={"BackupVaultName": VAULT},
    )
    with stubber:
        assert cleanup.delete_vault() is False


def _invoke(cleanup: mock.Mock, args: list[str], **kwargs):
    settings = mock.Mock(region=REGION)
    with (
        mock.patch(
            "pocket_cli.cli.cleanup_deprecated_cli.Settings.from_toml",
            return_value=settings,
        ),
        mock.patch(
            "pocket_cli.cli.cleanup_deprecated_cli.LegacyBackupCleanup",
            return_value=cleanup,
        ),
    ):
        return CliRunner().invoke(
            cleanup_deprecated, ["--stage", "dev", *args], **kwargs
        )


def _cleanup_mock(*, references=(), vault=True, role=True, points=()):
    cleanup = mock.Mock()
    cleanup.references.return_value = list(references)
    cleanup.vault_exists.return_value = vault
    cleanup.role_exists.return_value = role
    cleanup.recovery_points.return_value = list(points)
    cleanup.last_expiry.return_value = datetime(2027, 1, 1)
    cleanup.delete_vault.return_value = True
    cleanup.delete_role.return_value = True
    return cleanup


def test_cli_aborts_while_any_stage_still_references_legacy():
    cleanup = _cleanup_mock(references=["backup plan p (保存先が pocket-backup)"])
    result = _invoke(cleanup, ["-y"])
    assert result.exit_code != 0
    assert "0.37.0 以上" in result.output
    cleanup.delete_vault.assert_not_called()
    cleanup.delete_role.assert_not_called()


def test_cli_nothing_to_clean():
    cleanup = _cleanup_mock(vault=False, role=False)
    result = _invoke(cleanup, [])
    assert result.exit_code == 0, result.output
    cleanup.recovery_points.assert_not_called()


def test_cli_deletes_empty_vault_and_role():
    cleanup = _cleanup_mock()
    result = _invoke(cleanup, ["-y"])
    assert result.exit_code == 0, result.output
    cleanup.delete_recovery_points.assert_not_called()
    cleanup.delete_vault.assert_called_once_with()
    cleanup.delete_role.assert_called_once_with()


def test_cli_keeps_vault_with_data_but_deletes_role():
    """データが残る vault は既定では消さない (失効予定日を案内)。ロールは消す。"""
    cleanup = _cleanup_mock(points=[{"RecoveryPointArn": RP_ARN % 1}])
    result = _invoke(cleanup, ["-y"])
    assert result.exit_code == 0, result.output
    assert "2027-01-01" in result.output
    cleanup.delete_recovery_points.assert_not_called()
    cleanup.delete_vault.assert_not_called()
    cleanup.delete_role.assert_called_once_with()


def test_cli_delete_recovery_points_is_explicit_opt_in():
    points = [{"RecoveryPointArn": RP_ARN % 1}]
    cleanup = _cleanup_mock(points=points)
    result = _invoke(cleanup, ["--delete-recovery-points", "-y"])
    assert result.exit_code == 0, result.output
    cleanup.delete_recovery_points.assert_called_once_with(points)
    cleanup.delete_vault.assert_called_once_with()


def test_cli_waits_for_recovery_point_deletion_before_vault(monkeypatch):
    """recovery point 削除の直後は vault を消せない (実測) ので反映を待つ。"""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    points = [{"RecoveryPointArn": RP_ARN % 1}]
    cleanup = _cleanup_mock(points=points)
    cleanup.delete_vault.side_effect = [False, False, True]
    result = _invoke(cleanup, ["--delete-recovery-points", "-y"])
    assert result.exit_code == 0, result.output
    assert cleanup.delete_vault.call_count == 3


def test_cli_reports_failure_when_vault_never_empties(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    cleanup = _cleanup_mock(points=[{"RecoveryPointArn": RP_ARN % 1}])
    cleanup.delete_vault.return_value = False
    result = _invoke(cleanup, ["--delete-recovery-points", "-y"])
    assert result.exit_code != 0
    assert "完了" not in result.output
    cleanup.delete_role.assert_called_once_with()  # ロールは先に消し終えている


def test_cli_declined_confirm_deletes_nothing():
    cleanup = _cleanup_mock()
    result = _invoke(cleanup, [], input="n\n")
    assert result.exit_code != 0
    cleanup.delete_vault.assert_not_called()
    cleanup.delete_role.assert_not_called()


def test_cli_dry_run_deletes_nothing():
    cleanup = _cleanup_mock()
    result = _invoke(cleanup, ["--dry-run"])
    assert result.exit_code == 0, result.output
    cleanup.delete_vault.assert_not_called()
    cleanup.delete_role.assert_not_called()
