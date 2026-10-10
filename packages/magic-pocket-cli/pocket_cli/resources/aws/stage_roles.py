"""stage で pocket が使う IAM role の一覧 ([iam] external_roles の事前作成対象)。

`pocket permissions roles` の出力と deploy 前の検査が同じ一覧を使う。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import boto3

from pocket_cli.resources.aws import iam_roles
from pocket_cli.resources.aws.builders.codebuild import CodeBuildBuilder
from pocket_cli.resources.aws.cloudformation import ContainerStack
from pocket_cli.resources.aws.state import create_state_store

if TYPE_CHECKING:
    from pocket.context import Context


def stage_role_specs(context: Context, *, account_id: str) -> list[iam_roles.RoleSpec]:
    """stage の deploy / backup で使う role を定義順 (container → CodeBuild →
    AWS Backup) で返す。AWS への問い合わせはしない。

    external_roles では role は種別単位なので、container ごとの Lambda / scheduler
    の定義を同名で 1 つにまとめる (権限は和集合)。"""
    specs: list[iam_roles.RoleSpec] = []
    for name in sorted(context.container):
        stack = ContainerStack(
            context.container[name],
            rds_context=context.rds,
            dsql_context=context.dsql,
            scheduler_context=context.scheduler.get(name),
        )
        specs.extend(stack.role_specs)
    codebuild = _codebuild_builder(context)
    if codebuild:
        specs.append(codebuild.role_spec(account_id))
    backup = _backup_role(context)
    if backup:
        specs.append(backup)
    return iam_roles.merge_role_specs(specs)


def verify_stage_roles(context: Context) -> None:
    """external_roles の role がすべて存在し pocket の service を信頼しているかを
    deploy の変更前に検査する (policy の中身は見ない)。"""
    if not context.external_roles or not context.general:
        return
    region = context.general.region
    account_id = boto3.client("sts", region_name=region).get_caller_identity()[
        "Account"
    ]
    iam = boto3.client("iam", region_name=region)
    for spec in stage_role_specs(context, account_id=account_id):
        iam_roles.verify_role(iam, spec)


def _codebuild_builder(context: Context) -> CodeBuildBuilder | None:
    containers = [
        c for c in context.container.values() if c.build.backend == "codebuild"
    ]
    if not containers or not context.general:
        return None
    # CodeBuild project / role は stage で 1 つ (container 間で共有)
    first = containers[0]
    return CodeBuildBuilder(
        region=context.general.region,
        resource_prefix=first.resource_prefix,
        state_bucket=create_state_store(context).bucket_name,
        compute_type=first.build.compute_type,
        permissions_boundary=first.permissions_boundary,
        external_roles=context.external_roles,
        external_role_prefix=context.external_role_prefix,
    )


def _backup_role(context: Context) -> iam_roles.RoleSpec | None:
    """AWS Backup のサービスロール。dsql はオンデマンドバックアップ / restore で
    [backup] の宣言が無くても使う。rds は [backup.rds] を宣言した時だけ。"""
    if context.dsql:
        return iam_roles.backup_role(
            context.dsql.backup_role_name,
            permissions_boundary=context.dsql.permissions_boundary,
            external=context.external_roles,
        )
    if context.backup and context.backup.plans:
        return iam_roles.backup_role(
            context.backup.role_name,
            permissions_boundary=context.backup.permissions_boundary,
            external=context.external_roles,
        )
    return None
