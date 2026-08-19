import boto3
import click

from pocket.context import Context
from pocket.utils import echo
from pocket_cli.resources.aws.builders.codebuild import CodeBuildBuilder
from pocket_cli.resources.aws.state import (
    context_resource_prefix,
    create_state_store,
)
from pocket_cli.resources.backup import Backup
from pocket_cli.resources.cloudfront import CloudFront
from pocket_cli.resources.cloudfront_acm import CloudFrontAcm
from pocket_cli.resources.cloudfront_keys import CloudFrontKeys
from pocket_cli.resources.container import Container
from pocket_cli.resources.dsql import Dsql
from pocket_cli.resources.neon import Neon
from pocket_cli.resources.rds import Rds
from pocket_cli.resources.s3 import S3
from pocket_cli.resources.tidb import TiDb
from pocket_cli.resources.upstash import Upstash
from pocket_cli.resources.vpc import Vpc


def _create_codebuild_builder(context: Context) -> CodeBuildBuilder | None:
    """CodeBuildBuilder インスタンスを作成（リソース確認用）"""
    if not context.container or not context.general:
        return None
    resource_prefix = context_resource_prefix(context)
    permissions_boundary = None
    for c_name in sorted(context.container):
        if context.container[c_name].permissions_boundary:
            permissions_boundary = context.container[c_name].permissions_boundary
            break
    return CodeBuildBuilder(
        region=context.general.region,
        resource_prefix=resource_prefix,
        state_bucket=f"{resource_prefix}state",
        permissions_boundary=permissions_boundary,
    )


def _container_vpc_contexts(context: Context) -> list:
    """container が参照する VPC context の一覧 (name で重複排除)。"""
    result = []
    seen: set[str] = set()
    for c_name in sorted(context.container):
        vpc_ctx = context.container[c_name].vpc
        if vpc_ctx and vpc_ctx.name not in seen:
            seen.add(vpc_ctx.name)
            result.append(vpc_ctx)
    return result


def _collect_vpc_targets(context: Context) -> list[str]:
    """VPC 関連の削除対象を収集"""
    targets: list[str] = []
    for vpc_ctx in _container_vpc_contexts(context):
        if not vpc_ctx.manage:
            targets.append("VPC (外部 VPC consumer タグ削除)")
            continue
        vpc = Vpc(vpc_ctx)
        vpc_parts = []
        if vpc.stack.status != "NOEXIST":
            vpc_parts.append("CFNスタック")
            if vpc.stack.consumers:
                vpc_parts.append("consumers: %s" % ", ".join(vpc.stack.consumers))
        if vpc.efs and vpc.efs.exists():
            vpc_parts.append("EFS")
        if vpc_parts:
            targets.append("VPC (%s)" % " + ".join(vpc_parts))
    return targets


def _collect_container_targets(context: Context, with_secrets: bool):
    """Container 関連の削除対象を収集"""
    targets: list[str] = []
    if not context.container:
        return targets

    for c_name in sorted(context.container):
        c_ctx = context.container[c_name]
        c = Container(c_ctx)
        parts = ["CFNスタック"]
        if c.ecr.exists():
            if c_ctx.ecr_name_overridden:
                parts.append("ECR は ecr_name 明示指定のため削除対象外")
            else:
                parts.append("ECR")
        targets.append("Container '%s' (%s)" % (c_name, " + ".join(parts)))

    parts = []
    has_secrets = context.secrets or any(c.secrets for c in context.container.values())
    if with_secrets and has_secrets:
        parts.append("secrets")

    # CodeBuildリソースの存在チェック（設定に関わらず）
    cb = _create_codebuild_builder(context)
    if cb and (cb.project_exists() or cb.role_exists()):
        parts.append("CodeBuild")

    if parts:
        targets.append("Container 共有リソース (%s)" % " + ".join(parts))

    return targets


def _warn_command_provisioned(label: str, hint: str):
    echo.warning(
        '%s は provisioning="command" のため destroy では削除しません。'
        "%s で削除してください。" % (label, hint)
    )


def _collect_aws_database_targets(context: Context) -> list[str]:
    targets: list[str] = []
    if context.backup:
        for plan_name in Backup(context.backup).existing_plan_names():
            targets.append(
                "AWS Backup plan: %s (recovery point と vault は残ります)" % plan_name
            )
    if context.dsql:
        dsql = Dsql(context.dsql)
        if dsql.status != "NOEXIST":
            targets.append("DSQL クラスター: %s" % context.dsql.tag_name)
    if context.rds:
        rds = Rds(context.rds)
        if rds.status != "NOEXIST":
            targets.append("RDS Aurora クラスター: %s" % context.rds.cluster_identifier)
    return targets


def _collect_external_database_targets(context: Context) -> list[str]:
    """外部 DB (TiDB / Upstash / Neon) の削除対象を収集

    provisioning="command" の DB は deploy 同様 destroy も管理しない
    (credential レス運用のため provider API を叩かない)。
    dsql/rds と同様に存在確認し、確認プロンプトの一覧を実態に合わせる。
    """
    targets: list[str] = []
    if context.tidb and context.tidb.provisioning != "command":
        if TiDb(context.tidb).cluster:
            targets.append("TiDB クラスタ")
    if context.upstash and context.upstash.provisioning != "command":
        if Upstash(context.upstash).database:
            targets.append("Upstash Redis: %s" % context.upstash.database_name)
    if context.neon and context.neon.provisioning != "command":
        neon = Neon(context.neon)
        if neon.branch:
            plan = neon.destroy_plan()
            if plan == "project":
                targets.append(
                    "Neon プロジェクト '%s' (root branch のため project ごと削除)"
                    % context.neon.project_name
                )
            elif plan == "branch":
                targets.append("Neon ブランチ")
            # "blocked" (root だが他 branch 同居) は削除できないため一覧に載せない
            # (destroy 実行時に警告する)
    return targets


def _collect_database_targets(context: Context) -> list[str]:
    """データベース関連の削除対象を収集"""
    return _collect_aws_database_targets(context) + _collect_external_database_targets(
        context
    )


def _collect_targets(context: Context, with_secrets: bool, with_state_bucket: bool):
    """削除対象のリソース一覧を収集"""
    targets: list[str] = []

    for name, cf_ctx in context.cloudfront.items():
        targets.append("CloudFront '%s' (CFNスタック + バケットポリシー)" % name)
        if cf_ctx.domain:
            targets.append("CloudFront ACM '%s' (us-east-1 証明書)" % name)

    targets.extend(_collect_container_targets(context, with_secrets))
    targets.extend(_collect_database_targets(context))
    targets.extend(_collect_vpc_targets(context))

    for name, cf_ctx in context.cloudfront.items():
        if cf_ctx.signing_key:
            targets.append("CloudFrontKeys '%s' (CFNスタック)" % name)

    if context.s3 and S3(context.s3).exists():
        targets.append("S3 バケット: %s" % context.s3.bucket_name)

    if with_state_bucket:
        targets.append("ステートバケット")

    return targets


def _destroy_codebuild(context: Context) -> None:
    """CodeBuildプロジェクト + IAMロールを削除（設定に関わらず存在すれば削除）"""
    cb = _create_codebuild_builder(context)
    if cb is None:
        return
    if cb.project_exists() or cb.role_exists():
        echo.log("Destroying CodeBuild resources...")
        cb.delete()
        echo.success("CodeBuild resources were deleted.")


def _destroy_log_groups(context: Context):
    """Lambda が自動作成したロググループを削除"""
    if not context.container:
        return
    for c_ctx in context.container.values():
        logs_client = boto3.client("logs", region_name=c_ctx.region)
        for handler_ctx in c_ctx.handlers.values():
            log_group_name = handler_ctx.log_group_name
            try:
                logs_client.delete_log_group(logGroupName=log_group_name)
                echo.log("Deleted log group: %s" % log_group_name)
            except logs_client.exceptions.ResourceNotFoundException:
                pass


def _destroy_containers(context: Context, with_secrets: bool):
    """Container 関連リソースを削除"""
    if not context.container:
        return

    for c_name in sorted(context.container):
        c_ctx = context.container[c_name]
        c = Container(c_ctx)
        if c.stack.status != "NOEXIST":
            echo.log("Destroying container '%s' stack..." % c_name)
            c.stack.delete()
            # Lambda が VPC 内にある場合、ENI 解放を含む削除完了を待たないと
            # 後続の VPC 削除が subnet 使用中で DELETE_FAILED になる
            c.stack.wait_status("NOEXIST", timeout=1800, interval=10)
            echo.success("Container '%s' stack was destroyed." % c_name)

        if c.ecr.exists():
            if c_ctx.ecr_name_overridden:
                echo.warning(
                    "ECR repository '%s' は ecr_name で明示指定されているため削除"
                    "しません (他 stage と共有の可能性があります)。"
                    "不要な場合は手動で削除してください。" % c_ctx.ecr_name
                )
            else:
                echo.log("Destroying ECR repository...")
                c.ecr.delete()
                echo.success("ECR repository was deleted.")

    _destroy_codebuild(context)

    _destroy_log_groups(context)

    if with_secrets:
        views = [context.secrets] + [
            context.container[n].secrets for n in sorted(context.container)
        ]
        views = [sc for sc in views if sc is not None]
        if views:
            echo.log("Destroying pocket managed secrets...")
            for sc in views:
                sc.pocket_store.delete_secrets()
            echo.success("Pocket managed secrets were deleted.")


def _destroy_backup(context: Context, yes: bool):
    """backup plan / selection を削除し、recovery point の扱いを案内する。

    plan は [backup] 宣言の有無に関わらず削除する (宣言を外した後の destroy でも
    残さない)。selection が削除済み cluster の ARN を指したまま残ると以後の
    backup job が失敗し続けるため、cluster 削除より先に消す。

    recovery point (バックアップデータ) は既定では消さない。deletable = true の
    宣言下で、対話の [y/N] に明示的に yes と答えた場合のみ削除する (--yes での
    一括承認では削除しない。データ削除だけは暗黙に通さない)。cluster 削除後は
    ARN で引けなくなるため、この確認も cluster 削除より先に行う。
    """
    if not context.backup:
        return
    backup = Backup(context.backup, dsql_context=context.dsql, rds_context=context.rds)
    backup.delete()
    points = backup.list_recovery_points()
    if not points:  # None (権限なし) or 空
        return
    if context.backup.declared and context.backup.deletable and not yes:
        echo.warning(
            "バックアップデータ (recovery point) が %d 件あります。" % len(points)
        )
        if click.confirm("recovery point も削除しますか？", default=False):
            backup.delete_recovery_points(points)
            return
    echo.warning(
        "バックアップデータ (recovery point %d 件) は削除されず残ります"
        " (保持期限まで課金対象)。削除するには [backup] deletable = true を"
        " 宣言して `pocket backup cleanup` を実行するか、AWS Backup の"
        " コンソールから削除してください。" % len(points)
    )


def _destroy_dsql(context: Context):
    """DSQL クラスターを削除"""
    if not context.dsql:
        return
    dsql = Dsql(context.dsql)
    if dsql.status == "NOEXIST":
        return
    echo.log("Destroying DSQL cluster...")
    dsql.delete()


def _destroy_rds(context: Context):
    """RDS Aurora クラスターを削除"""
    if not context.rds:
        return
    rds = Rds(context.rds)
    if rds.status == "NOEXIST":
        return
    echo.log("Destroying RDS Aurora cluster...")
    rds.delete()
    echo.success("RDS Aurora cluster was destroyed. Final snapshot was created.")


def _destroy_vpc(context: Context):
    """VPC 関連リソースを削除"""
    for vpc_ctx in _container_vpc_contexts(context):
        if not vpc_ctx.manage:
            # 外部 VPC: consumer タグのみ削除
            from pocket_cli.resources.aws.cloudformation import VpcStack

            vpc_stack = VpcStack(vpc_ctx)
            if vpc_stack.status != "NOEXIST":
                slug = context.stage + "-" + context.project_name
                vpc_stack.remove_consumer_tag(slug)
                echo.log("外部 VPC の consumer タグを削除しました。")
            continue
        # managed VPC: consumer チェック後に削除
        vpc = Vpc(vpc_ctx)
        if vpc.stack.consumers:
            echo.danger("VPC に consumer がいるため削除できません:")
            for c in vpc.stack.consumers:
                echo.info("  - %s" % c)
            continue
        has_stack = vpc.stack.status != "NOEXIST"
        has_efs = vpc.efs and vpc.efs.exists()
        if has_stack or has_efs:
            echo.log("Destroying VPC...")
            vpc.delete()
            echo.success("VPC was destroyed.")


def _destroy_tidb(context: Context):
    """TiDB クラスタを削除 (provisioning="command" は管理外)"""
    if not context.tidb:
        return
    if context.tidb.provisioning == "command":
        _warn_command_provisioned("TiDB", "pocket resource tidb delete")
        return
    if TiDb(context.tidb).cluster:
        echo.log("Destroying TiDB cluster...")
        TiDb(context.tidb).delete_cluster()
        echo.success("TiDB cluster was deleted.")


def _destroy_upstash(context: Context):
    """Upstash Redis を削除 (provisioning="command" は管理外)"""
    if not context.upstash:
        return
    if context.upstash.provisioning == "command":
        _warn_command_provisioned("Upstash", "Upstash コンソール等")
        return
    upstash = Upstash(context.upstash)
    if upstash.database:
        upstash.delete_database()


def _destroy_neon(context: Context):
    """Neon ブランチを削除 (provisioning="command" は管理外)

    root branch は Neon 仕様で branch 単位の削除ができない (422: cannot delete the
    root branch) ため、project 内に他 branch が無ければ project ごと削除する。他
    branch が同居している場合は巻き添えになるため何も消さず警告して続行する
    (destroy 全体を異常終了させない)。
    """
    if not context.neon:
        return
    if context.neon.provisioning == "command":
        _warn_command_provisioned("Neon", "pocket resource neon delete")
        return
    neon = Neon(context.neon)
    if not neon.branch:
        return
    plan = neon.destroy_plan()
    if plan == "blocked":
        echo.warning(
            "Neon branch '%s' は root branch のため単体削除できず、project '%s' には"
            "他の branch が残っているため project 削除も行いません。"
            "他 stage の destroy 後に pocket resource neon delete で削除してください。"
            % (context.neon.branch_name, context.neon.project_name)
        )
        return
    if plan == "project":
        echo.log("Destroying Neon project (root branch のため project ごと削除)...")
        neon.delete_project()
        echo.success("Neon project '%s' was deleted." % context.neon.project_name)
        return
    echo.log("Destroying Neon branch...")
    neon.delete_branch()
    echo.success("Neon branch was deleted.")


def _destroy_cloudfront_and_acm(context: Context):
    """CloudFront ディストリビューションと ACM 証明書を削除"""
    for name, cf_ctx in context.cloudfront.items():
        cf = CloudFront(cf_ctx)
        if cf.stack.status != "NOEXIST":
            echo.log("Destroying CloudFront '%s'..." % name)
            cf.delete()
            echo.success("CloudFront '%s' was destroyed." % name)
        if cf_ctx.domain:
            acm = CloudFrontAcm(cf_ctx)
            if acm.stack.status != "NOEXIST":
                echo.log("Destroying CloudFront ACM '%s'..." % name)
                acm.delete()
                echo.success("CloudFront ACM '%s' was destroyed." % name)


def _destroy_resources(
    context: Context,
    with_secrets: bool,
    with_state_bucket: bool,
    yes: bool = False,
):
    """リソースをデプロイの逆順で削除"""
    # 1. CloudFront + ACM
    _destroy_cloudfront_and_acm(context)

    # 2. Container (CFNスタック + ECR + secrets)
    _destroy_containers(context, with_secrets)

    # 2.4. backup plan（DSQL / RDS の cluster 削除より先）
    _destroy_backup(context, yes)

    # 2.5. DSQL
    _destroy_dsql(context)

    # 2.6. RDS（Container の後、VPC の前）
    _destroy_rds(context)

    # 3. VPC（RDS の後。RDS が VPC の subnet / SG を使用しているため、
    #    先に VPC を消すと DELETE_FAILED になる）
    _destroy_vpc(context)

    # 3.5. CloudFrontKeys（Container の後、S3 の前）
    for name, cf_ctx in context.cloudfront.items():
        if cf_ctx.signing_key:
            cfk = CloudFrontKeys(cf_ctx)
            if cfk.stack.status != "NOEXIST":
                echo.log("Destroying CloudFrontKeys '%s'..." % name)
                cfk.delete()
                echo.success("CloudFrontKeys '%s' was destroyed." % name)

    # 4. S3 バケット
    if context.s3 and S3(context.s3).exists():
        echo.log("Destroying S3 bucket...")
        S3(context.s3).delete()
        echo.success("S3 bucket was deleted.")

    # 5. TiDB クラスタ (provisioning="command" は deploy 同様 destroy も管理しない)
    _destroy_tidb(context)

    # 5.5. Upstash Redis
    _destroy_upstash(context)

    # 6. Neon ブランチ
    _destroy_neon(context)

    # 7. ステートバケット
    if with_state_bucket:
        state_store = create_state_store(context)
        echo.log("Destroying state bucket...")
        state_store.delete_bucket()
        echo.success("State bucket was deleted.")


@click.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option(
    "--without-secrets",
    is_flag=True,
    default=False,
    help="シークレットを削除せずに残す",
)
@click.option("--with-state-bucket", is_flag=True, default=False)
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="確認プロンプトをスキップ"
)
def destroy(stage: str, without_secrets: bool, with_state_bucket: bool, yes: bool):
    """ステージの全リソースを一括削除"""
    from pocket_cli.cli.aws_auth import check_aws_credentials

    check_aws_credentials()
    context = Context.from_toml(stage=stage)
    with_secrets = not without_secrets
    targets = _collect_targets(context, with_secrets, with_state_bucket)

    if not targets:
        echo.warning("削除対象のリソースが見つかりません。")
        return

    echo.danger("以下のリソースを削除します:")
    for target in targets:
        echo.info("  - %s" % target)
    echo.danger("この操作は取り消せません！")
    if not yes:
        click.confirm("stage '%s' の全リソースを削除しますか？" % stage, abort=True)

    _destroy_resources(context, with_secrets, with_state_bucket, yes=yes)
    echo.success("stage '%s' の全リソースを削除しました。" % stage)
