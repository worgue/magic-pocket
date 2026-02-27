import click

from pocket.context import Context
from pocket.utils import echo
from pocket_cli.resources.aws.builders.codebuild import CodeBuildBuilder
from pocket_cli.resources.aws.state import StateStore
from pocket_cli.resources.awscontainer import AwsContainer
from pocket_cli.resources.cloudfront import CloudFront
from pocket_cli.resources.cloudfront_keys import CloudFrontKeys
from pocket_cli.resources.neon import Neon
from pocket_cli.resources.s3 import S3
from pocket_cli.resources.tidb import TiDb
from pocket_cli.resources.vpc import Vpc


def _create_state_store(context: Context) -> StateStore:
    assert context.general
    resource_prefix = context.general.prefix_template.format(
        stage=context.stage,
        project=context.project_name,
        namespace=context.general.namespace,
    )
    bucket_name = f"{resource_prefix}state"
    return StateStore(bucket_name, context.general.region)


def _create_codebuild_builder(context: Context) -> CodeBuildBuilder | None:
    """CodeBuildBuilder インスタンスを作成（リソース確認用）"""
    if not context.awscontainer or not context.general:
        return None
    resource_prefix = context.general.prefix_template.format(
        stage=context.stage,
        project=context.project_name,
        namespace=context.general.namespace,
    )
    state_bucket = f"{resource_prefix}state"
    return CodeBuildBuilder(
        region=context.general.region,
        resource_prefix=resource_prefix,
        state_bucket=state_bucket,
        permissions_boundary=context.awscontainer.permissions_boundary,
    )


def _collect_awscontainer_targets(context: Context, with_secrets: bool):
    """AwsContainer 関連の削除対象を収集"""
    targets: list[str] = []
    if not context.awscontainer:
        return targets

    ac = AwsContainer(context.awscontainer)
    parts = ["CFNスタック"]
    if ac.ecr.exists():
        parts.append("ECR")
    if with_secrets and context.awscontainer.secrets:
        parts.append("secrets")

    # CodeBuildリソースの存在チェック（設定に関わらず）
    cb = _create_codebuild_builder(context)
    if cb and (cb.project_exists() or cb.role_exists()):
        parts.append("CodeBuild")

    targets.append("AwsContainer (%s)" % " + ".join(parts))

    if context.awscontainer.vpc:
        vpc = Vpc(context.awscontainer.vpc)
        vpc_parts = []
        if vpc.stack.status != "NOEXIST":
            vpc_parts.append("CFNスタック")
        if vpc.efs and vpc.efs.exists():
            vpc_parts.append("EFS")
        if vpc_parts:
            targets.append("VPC (%s)" % " + ".join(vpc_parts))

    return targets


def _collect_targets(context: Context, with_secrets: bool, with_state_bucket: bool):
    """削除対象のリソース一覧を収集"""
    targets: list[str] = []

    for name in context.cloudfront:
        targets.append("CloudFront '%s' (CFNスタック + バケットポリシー)" % name)

    targets.extend(_collect_awscontainer_targets(context, with_secrets))

    for name, cf_ctx in context.cloudfront.items():
        if cf_ctx.signing_key:
            targets.append("CloudFrontKeys '%s' (CFNスタック)" % name)

    if context.s3 and S3(context.s3).exists():
        targets.append("S3 バケット: %s" % context.s3.bucket_name)

    if context.tidb:
        targets.append("TiDB クラスタ")

    if context.neon:
        targets.append("Neon ブランチ")

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


def _destroy_awscontainer(context: Context, with_secrets: bool):
    """AwsContainer 関連リソースを削除"""
    if not context.awscontainer:
        return

    ac = AwsContainer(context.awscontainer)
    if ac.stack.status != "NOEXIST":
        echo.log("Destroying AwsContainer stack...")
        ac.stack.delete()
        echo.success("AwsContainer stack was destroyed.")

    if ac.ecr.exists():
        echo.log("Destroying ECR repository...")
        ac.ecr.delete()
        echo.success("ECR repository was deleted.")

    _destroy_codebuild(context)

    if with_secrets and context.awscontainer.secrets:
        echo.log("Destroying pocket managed secrets...")
        context.awscontainer.secrets.pocket_store.delete_secrets()
        echo.success("Pocket managed secrets were deleted.")

    _destroy_vpc(context)


def _destroy_vpc(context: Context):
    """VPC 関連リソースを削除"""
    if not context.awscontainer or not context.awscontainer.vpc:
        return
    vpc = Vpc(context.awscontainer.vpc)
    has_stack = vpc.stack.status != "NOEXIST"
    has_efs = vpc.efs and vpc.efs.exists()
    if has_stack or has_efs:
        echo.log("Destroying VPC...")
        vpc.delete()
        echo.success("VPC was destroyed.")


def _destroy_resources(context: Context, with_secrets: bool, with_state_bucket: bool):
    """リソースをデプロイの逆順で削除"""
    # 1. CloudFront
    for name, cf_ctx in context.cloudfront.items():
        cf = CloudFront(cf_ctx)
        if cf.stack.status != "NOEXIST":
            echo.log("Destroying CloudFront '%s'..." % name)
            cf.delete()
            echo.success("CloudFront '%s' was destroyed." % name)

    # 2. AwsContainer (CFNスタック + ECR + secrets) + 3. VPC
    _destroy_awscontainer(context, with_secrets)

    # 3.5. CloudFrontKeys（AwsContainer の後、S3 の前）
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

    # 5. TiDB クラスタ
    if context.tidb and TiDb(context.tidb).cluster:
        echo.log("Destroying TiDB cluster...")
        TiDb(context.tidb).delete_cluster()
        echo.success("TiDB cluster was deleted.")

    # 6. Neon ブランチ
    if context.neon and Neon(context.neon).branch:
        echo.log("Destroying Neon branch...")
        Neon(context.neon).delete_branch()
        echo.success("Neon branch was deleted.")

    # 7. ステートバケット
    if with_state_bucket:
        state_store = _create_state_store(context)
        echo.log("Destroying state bucket...")
        state_store.delete_bucket()
        echo.success("State bucket was deleted.")


@click.command()
@click.option("--stage", envvar="POCKET_STAGE", prompt=True)
@click.option("--with-secrets", is_flag=True, default=False)
@click.option("--with-state-bucket", is_flag=True, default=False)
def destroy(stage: str, with_secrets: bool, with_state_bucket: bool):
    """ステージの全リソースを一括削除"""
    context = Context.from_toml(stage=stage)
    targets = _collect_targets(context, with_secrets, with_state_bucket)

    if not targets:
        echo.warning("削除対象のリソースが見つかりません。")
        return

    echo.danger("以下のリソースを削除します:")
    for target in targets:
        echo.info("  - %s" % target)
    echo.danger("この操作は取り消せません！")
    click.confirm("stage '%s' の全リソースを削除しますか？" % stage, abort=True)

    _destroy_resources(context, with_secrets, with_state_bucket)
    echo.success("stage '%s' の全リソースを削除しました。" % stage)
