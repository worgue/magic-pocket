from __future__ import annotations

from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from pocket.resources.base import ResourceStatus
from pocket.utils import echo
from pocket_cli.resources.aws.backup_common import (
    BACKUP_VAULT_NAME,
    ensure_backup_role,
    ensure_backup_vault,
)
from pocket_cli.resources.dsql import Dsql
from pocket_cli.resources.rds import Rds

if TYPE_CHECKING:
    from pocket.context import (
        BackupContext,
        BackupPlanContext,
        DsqlContext,
        RdsContext,
    )

# backup plan の drift 判定で比較する項目。AWS が既定値で補完する項目
# (StartWindowMinutes / RuleId 等) を含めると毎 deploy で update が走るため、
# pocket が宣言した項目だけを見る
_PLAN_RULE_KEYS = (
    "TargetBackupVaultName",
    "ScheduleExpression",
    "ScheduleExpressionTimezone",
)
_PLAN_LIFECYCLE_KEYS = ("MoveToColdStorageAfterDays", "DeleteAfterDays")


def _rules_by_name(rules: list[dict]) -> dict[str, dict]:
    return {rule.get("RuleName", ""): rule for rule in rules}


def _plan_matches(current: dict, desired: dict) -> bool:
    """rule 名で突き合わせて宣言項目を比較する (GetBackupPlan の順序に依存しない)。"""
    current_rules = _rules_by_name(current.get("Rules", []))
    desired_rules = _rules_by_name(desired["Rules"])
    if set(current_rules) != set(desired_rules):
        return False
    for name, want in desired_rules.items():
        cur = current_rules[name]
        if any(cur.get(key) != want.get(key) for key in _PLAN_RULE_KEYS):
            return False
        cur_lifecycle = cur.get("Lifecycle", {})
        want_lifecycle = want["Lifecycle"]
        if any(
            cur_lifecycle.get(key) != want_lifecycle.get(key)
            for key in _PLAN_LIFECYCLE_KEYS
        ):
            return False
    return True


class Backup:
    """[backup.dsql] / [backup.rds] 宣言に沿った AWS Backup plan の管理。

    エンジンごとに rule 構成が違う (rds は daily を PITR が担う / cold storage
    不可) ため、plan はエンジン別に provision する。plan 管理は cluster の
    作成後に走る必要があるため、実作業は ensure_post_deploy_state で行う
    (status は常に COMPLETED)。

    バックアップ「データ」(recovery point) は原則触らない。削除できるのは
    [backup] deletable = true の宣言下で、利用者が明示的に確認した場合のみ
    (`pocket backup cleanup` / destroy の確認プロンプト)。
    """

    context: BackupContext

    def __init__(
        self,
        context: BackupContext,
        dsql_context: DsqlContext | None = None,
        rds_context: RdsContext | None = None,
    ) -> None:
        self.context = context
        self._dsql_context = dsql_context
        self._rds_context = rds_context
        self._backup = boto3.client("backup", region_name=context.region)
        self._iam = boto3.client("iam", region_name=context.region)

    @property
    def status(self) -> ResourceStatus:
        # plan は cluster 作成後でないと selection を張れないため、作成・更新は
        # ensure_post_deploy_state で冪等に行う
        return "COMPLETED"

    @property
    def description(self):
        return "Configure AWS Backup plans: %s" % ", ".join(
            plan.plan_name for plan in self.context.plans
        )

    def deploy_init(self):
        pass

    def create(self):
        self.ensure_post_deploy_state()

    def update(self):
        self.ensure_post_deploy_state()

    def state_info(self):
        return {
            "backup": {
                "plans": [plan.plan_name for plan in self.context.plans],
            }
        }

    def _service_target_arn(self, service: str) -> str | None:
        if service == "dsql" and self._dsql_context:
            return Dsql(self._dsql_context).backup_target_arn
        if service == "rds" and self._rds_context:
            return Rds(self._rds_context).backup_target_arn
        return None

    def target_arns(self) -> list[str]:
        """recovery point 操作の対象 ARN 一覧 (未作成 cluster は除く)。

        [backup] の宣言に依らず、stage に存在する対象 DB 全部を返す
        (destroy 時のデータ残存案内は宣言を外した後も出すため)。
        """
        arns = [self._service_target_arn(s) for s in ("dsql", "rds")]
        return sorted(arn for arn in arns if arn is not None)

    def ensure_post_deploy_state(self):
        """宣言されたエンジンごとに plan / selection を冪等に確保する。

        未宣言なら何もしない (opt-in。宣言を外した後の既存 plan にも触らない。
        plan の掃除は destroy の責務)。
        """
        if not self.context.plans:
            return
        try:
            targets = [
                (plan, arn)
                for plan in self.context.plans
                if (arn := self._service_target_arn(plan.service)) is not None
            ]
            if not targets:
                return
            ensure_backup_vault(self._backup, BACKUP_VAULT_NAME)
            role_arn = ensure_backup_role(self._iam, self.context.permissions_boundary)
            for plan, arn in targets:
                plan_id = self._ensure_plan(plan)
                self._ensure_selection(plan, plan_id, role_arn, [arn])
        except ClientError as e:
            if e.response["Error"]["Code"] != "AccessDeniedException":
                raise
            echo.warning(
                "AWS Backup の設定に必要な権限が無いため、定期バックアップの"
                " provisioning をスキップしました: %s" % e
            )
            echo.warning(
                "  deploy role に backup:* を付与してください"
                " (`pocket permissions list` 参照)。"
            )

    def _plan_document(self, plan: BackupPlanContext) -> dict:
        rules = []
        for rule in plan.rules:
            lifecycle: dict = {"DeleteAfterDays": rule.delete_after_days}
            if rule.cold_storage_after_days:
                lifecycle["MoveToColdStorageAfterDays"] = rule.cold_storage_after_days
            rules.append(
                {
                    "RuleName": rule.name,
                    "TargetBackupVaultName": BACKUP_VAULT_NAME,
                    "ScheduleExpression": rule.schedule_expression,
                    "ScheduleExpressionTimezone": self.context.timezone,
                    "Lifecycle": lifecycle,
                }
            )
        return {"BackupPlanName": plan.plan_name, "Rules": rules}

    def _find_plan_id(self, plan_name: str) -> str | None:
        paginator = self._backup.get_paginator("list_backup_plans")
        for page in paginator.paginate():
            for plan in page["BackupPlansList"]:
                if plan["BackupPlanName"] == plan_name:
                    return plan["BackupPlanId"]
        return None

    def existing_plan_names(self) -> list[str]:
        """destroy の対象一覧表示用 (権限が無ければ「無し」扱い)。"""
        try:
            return [
                name
                for name in self.context.cleanup_plan_names
                if self._find_plan_id(name) is not None
            ]
        except ClientError as e:
            if e.response["Error"]["Code"] != "AccessDeniedException":
                raise
            return []

    def _ensure_plan(self, plan: BackupPlanContext) -> str:
        document = self._plan_document(plan)
        plan_id = self._find_plan_id(plan.plan_name)
        if plan_id is None:
            echo.log("Creating backup plan: %s" % plan.plan_name)
            return self._backup.create_backup_plan(BackupPlan=document)["BackupPlanId"]
        # 宣言が変わっていれば追従する (cron / lifecycle の drift 収束)
        current = self._backup.get_backup_plan(BackupPlanId=plan_id)["BackupPlan"]
        if not _plan_matches(current, document):
            echo.log("Updating backup plan: %s" % plan.plan_name)
            self._backup.update_backup_plan(BackupPlanId=plan_id, BackupPlan=document)
        return plan_id

    def _ensure_selection(
        self,
        plan: BackupPlanContext,
        plan_id: str,
        role_arn: str,
        arns: list[str],
    ) -> None:
        """対象 DB の ARN を指した selection を確保する。

        selection には更新 API が無いため、対象 ARN が変わっていたら
        (cluster 再作成など) 作り直す。
        """
        selections = self._backup.list_backup_selections(BackupPlanId=plan_id)
        for item in selections["BackupSelectionsList"]:
            if item["SelectionName"] != plan.plan_name:
                continue
            detail = self._backup.get_backup_selection(
                BackupPlanId=plan_id, SelectionId=item["SelectionId"]
            )
            if sorted(detail["BackupSelection"].get("Resources", [])) == arns:
                return
            self._backup.delete_backup_selection(
                BackupPlanId=plan_id, SelectionId=item["SelectionId"]
            )
        echo.log("Creating backup selection for %s" % plan.plan_name)
        self._backup.create_backup_selection(
            BackupPlanId=plan_id,
            BackupSelection={
                "SelectionName": plan.plan_name,
                "IamRoleArn": role_arn,
                "Resources": arns,
            },
        )

    def delete(self) -> None:
        """backup plan / selection を削除する。

        recovery point と vault は消さない。バックアップ「設定」は stack の
        付属物だが、バックアップ「データ」は stack より長生きさせるべきで、
        cluster を消した後こそ必要になりうるため。

        [backup] の宣言の有無に関わらず、stage に存在しうる全 plan 名を走査する
        (宣言を外した後の destroy でも plan を残さないため)。backup 権限を
        持たない deploy role でも destroy 自体は完遂させたいので、権限エラーは
        警告に留める。
        """
        for plan_name in self.context.cleanup_plan_names:
            try:
                plan_id = self._find_plan_id(plan_name)
            except ClientError as e:
                if e.response["Error"]["Code"] != "AccessDeniedException":
                    raise
                echo.warning(
                    "backup plan を確認する権限がないため、削除をスキップしました"
                    " (backup:ListBackupPlans)。plan が残っている場合は手動で"
                    " 削除してください。"
                )
                return
            if plan_id is None:
                continue
            selections = self._backup.list_backup_selections(BackupPlanId=plan_id)
            for item in selections["BackupSelectionsList"]:
                self._backup.delete_backup_selection(
                    BackupPlanId=plan_id, SelectionId=item["SelectionId"]
                )
            self._backup.delete_backup_plan(BackupPlanId=plan_id)
            echo.log("Deleted backup plan: %s" % plan_name)

    def list_recovery_points(self) -> list[dict] | None:
        """対象 DB の recovery point 一覧 (pocket-backup vault 内のもの)。

        現存する cluster の ARN でしか引けない (削除済み cluster の分は対象外 =
        console でしか消せない)。権限が無ければ None を返す (呼び出し側が案内を
        スキップできるように、空リストと区別する)。
        """
        points: list[dict] = []
        try:
            for arn in self.target_arns():
                paginator = self._backup.get_paginator(
                    "list_recovery_points_by_resource"
                )
                for page in paginator.paginate(ResourceArn=arn):
                    points.extend(page["RecoveryPoints"])
        except ClientError as e:
            if e.response["Error"]["Code"] != "AccessDeniedException":
                raise
            return None
        # pocket 管理の vault 分だけを扱う (--vault で利用者の vault に取った
        # オンデマンド分は利用者の管理物とみなし、pocket からは消さない)
        return [p for p in points if p.get("BackupVaultName") == BACKUP_VAULT_NAME]

    def delete_recovery_points(self, points: list[dict]) -> int:
        """recovery point を削除して件数を返す。

        [backup] deletable = true + 利用者の明示確認を経た経路 (cleanup CLI /
        destroy の確認プロンプト) からのみ呼ぶこと。
        """
        for point in points:
            self._backup.delete_recovery_point(
                BackupVaultName=point["BackupVaultName"],
                RecoveryPointArn=point["RecoveryPointArn"],
            )
        if points:
            echo.log("Deleted %d recovery point(s)." % len(points))
        return len(points)
