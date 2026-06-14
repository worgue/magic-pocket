import inspect
import webbrowser

import click

from pocket.context import Context
from pocket.utils import echo
from pocket_cli.mediator import Mediator
from pocket_cli.resources.aws.state import StateStore
from pocket_cli.resources.awscontainer import AwsContainer
from pocket_cli.resources.cloudfront import CloudFront
from pocket_cli.resources.cloudfront_acm import CloudFrontAcm
from pocket_cli.resources.cloudfront_keys import CloudFrontKeys
from pocket_cli.resources.cloudfront_waf import CloudFrontWaf
from pocket_cli.resources.dsql import Dsql
from pocket_cli.resources.neon import Neon
from pocket_cli.resources.rds import Rds
from pocket_cli.resources.s3 import S3
from pocket_cli.resources.tidb import TiDb
from pocket_cli.resources.upstash import Upstash
from pocket_cli.resources.vpc import Vpc


def _append_infra_resources(resources, context: Context, state_bucket: str):
    """VPC / RDS / CloudFrontKeys / AwsContainer をまとめて追加"""
    if context.awscontainer and context.awscontainer.vpc:
        if context.awscontainer.vpc.manage:
            resources.append(Vpc(context.awscontainer.vpc))
    if context.dsql:
        resources.append(Dsql(context.dsql))
    if context.rds:
        resources.append(Rds(context.rds))
    for _name, cf_ctx in context.cloudfront.items():
        if cf_ctx.signing_key:
            resources.append(CloudFrontKeys(cf_ctx))
    if context.awscontainer:
        resources.append(
            AwsContainer(
                context.awscontainer,
                state_bucket=state_bucket,
                rds_context=context.rds,
                dsql_context=context.dsql,
                scheduler_context=context.scheduler,
            )
        )


def get_resources(context: Context, *, state_bucket: str = ""):
    resources = []
    # ACM 証明書を最初にデプロイ（us-east-1、DNS 検証に時間がかかる）
    for _name, cf_ctx in context.cloudfront.items():
        if cf_ctx.domain:
            resources.append(CloudFrontAcm(cf_ctx))
    # WAF (IPSet + WebACL) も us-east-1 必須、CloudFront stack より前に作成
    for _name, cf_ctx in context.cloudfront.items():
        if cf_ctx.waf is not None:
            resources.append(CloudFrontWaf(cf_ctx))
    if context.neon:
        resources.append(Neon(context.neon))
    if context.tidb:
        resources.append(TiDb(context.tidb))
    if context.upstash:
        resources.append(Upstash(context.upstash))
    if context.s3:
        resources.append(S3(context.s3, cloudfront_contexts=context.cloudfront))
    _append_infra_resources(resources, context, state_bucket)
    for _name, cf_ctx in context.cloudfront.items():
        resources.append(CloudFront(cf_ctx))
    return resources


def _create_state_store(context: Context) -> StateStore:
    if not context.general:
        raise RuntimeError("general context is not configured")
    resource_prefix = context.general.prefix_template.format(
        stage=context.stage,
        project=context.project_name,
        namespace=context.general.namespace,
    )
    bucket_name = f"{resource_prefix}state"
    return StateStore(bucket_name, context.general.region)


def deploy_init_resources(context: Context, *, state_bucket: str = ""):
    for resource in get_resources(context, state_bucket=state_bucket):
        target_name = resource.__class__.__name__
        echo.log("Deploy init %s..." % target_name)
        resource.deploy_init()


def deploy_frontend(context: Context, *, skip_build: bool = False):
    for _name, cf_ctx in context.cloudfront.items():
        cf = CloudFront(cf_ctx)
        if not cf_ctx.uploadable_routes:
            continue
        if cf.status == "NOEXIST":
            echo.warning("CloudFront '%s' が未作成です。スキップします。" % cf_ctx.name)
            continue
        cf.upload(skip_build=skip_build)


def upload_managed_assets(context: Context):
    """CloudFront resource ごとに managed_assets を S3 に同期する。

    deploy_resources の後で呼ぶことで、CFn stack の有無に関わらず毎回実行される。
    差分検知 (ローカル MD5 vs S3 ETag) により変更ファイルのみ PutObject される。
    """
    for _name, cf_ctx in context.cloudfront.items():
        if not cf_ctx.managed_assets:
            continue
        cf = CloudFront(cf_ctx)
        cf.upload_managed_assets()


def deploy_resources(context: Context, *, state_bucket: str = ""):
    state_store = _create_state_store(context)
    # state bucket は deploy_init_resources の前に作成済み
    # ここでは念のため再確認
    state_store.ensure_bucket()

    mediator = Mediator(context)
    resources = get_resources(context, state_bucket=state_bucket)
    for resource in resources:
        target_name = resource.__class__.__name__
        if resource.status == "NOEXIST":
            echo.log("Creating %s..." % target_name)
            if "mediator" in inspect.signature(resource.create).parameters:
                resource.create(mediator)
            else:
                resource.create()
            state_store.record(resource.state_info())
        elif resource.status == "REQUIRE_UPDATE":
            echo.log("Updating %s..." % target_name)
            if "mediator" in inspect.signature(resource.update).parameters:
                resource.update(mediator)
            else:
                resource.update()
            state_store.record(resource.state_info())
        else:
            echo.log("%s is already the latest version." % target_name)
    # stack 作成/更新が終わった後の後付け状態 (bucket policy / KVS など) を
    # 冪等に確保する。wait_status が timeout した次の deploy でも復旧できる。
    for resource in resources:
        hook = getattr(resource, "ensure_post_deploy_state", None)
        if hook is None:
            continue
        if "mediator" in inspect.signature(hook).parameters:
            hook(mediator)
        else:
            hook()


def apply_skip_check_existing(context: Context) -> None:
    """DB リソース (neon/tidb/upstash) の存在確認を一律 skip させる。

    `--skip-check-existing` 指定時に呼ぶ。pocket.toml を編集せず、その deploy
    実行に限り外部 SaaS API への存在確認 call を回避する (deploy ロールに
    DB credentials を渡さず deploy を完走させる用途)。toml 側の
    `skip_check_existing` フラグと同義で、こちらは実行時上書き。
    """
    for db_ctx in (context.neon, context.tidb, context.upstash):
        if db_ctx is not None:
            db_ctx.skip_check_existing = True


def build_image(context: Context, *, tag: str) -> str:
    """awscontainer image を指定 tag で build & push する (deploy はしない)。

    build once 用。codebuild backend は source upload に state bucket を要するため、
    deploy と同様に先に state bucket を確保してから build する。戻り値は ecr_name:tag。
    """
    if context.awscontainer is None:
        raise click.ClickException("awscontainer がこの stage に設定されていません。")
    state_store = _create_state_store(context)
    state_store.ensure_bucket()
    ac = AwsContainer(context.awscontainer, state_bucket=state_store.bucket_name)
    ac.build(tag)
    return f"{context.awscontainer.ecr_name}:{tag}"


def _deploy_pipeline(context: Context, *, openpath=None, skip_frontend=False):
    """deploy / promote 共通のパイプライン本体。

    promote 時は context.awscontainer.promote_commit_hash が設定済みで、
    deploy_init 内の image build が retag に置き換わる以外は deploy と同一。
    """
    # CodeBuildがソースアップロードにstate bucketを必要とするため、先に作成
    state_store = _create_state_store(context)
    state_store.ensure_bucket()
    state_bucket = state_store.bucket_name
    deploy_init_resources(context, state_bucket=state_bucket)
    deploy_resources(context, state_bucket=state_bucket)
    upload_managed_assets(context)
    if not skip_frontend:
        deploy_frontend(context)
    # デプロイ完了後の URL 表示
    url = _get_deploy_url(context)
    if url:
        echo.success(f"url: {url}")
        if openpath:
            webbrowser.open(url + "/" + openpath)


@click.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--openpath")
@click.option("--skip-frontend", is_flag=True, default=False)
@click.option(
    "--skip-check-existing",
    is_flag=True,
    default=False,
    help="neon/tidb/upstash の存在確認 API を skip し COMPLETED 扱いで deploy",
)
def deploy(stage: str, openpath, skip_frontend, skip_check_existing):
    from pocket_cli.cli.aws_auth import check_aws_credentials

    check_aws_credentials()
    context = Context.from_toml(stage=stage)
    if skip_check_existing:
        apply_skip_check_existing(context)
    _deploy_pipeline(context, openpath=openpath, skip_frontend=skip_frontend)


@click.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--commit-hash", required=True, help="昇格する image の git commit hash")
@click.option("--openpath")
@click.option("--skip-frontend", is_flag=True, default=False)
@click.option(
    "--skip-check-existing",
    is_flag=True,
    default=False,
    help="neon/tidb/upstash の存在確認 API を skip し COMPLETED 扱いで deploy",
)
def promote(stage: str, commit_hash, openpath, skip_frontend, skip_check_existing):
    """build 済みの :<commit-hash> image へ stage を向けて deploy する (再ビルドなし)。

    `pocket django build` で push した image に :<stage> タグを移し、
    インフラ/Lambda を更新する。image build は行わない (build once の昇格)。
    """
    from pocket_cli.cli.aws_auth import check_aws_credentials

    check_aws_credentials()
    context = Context.from_toml(stage=stage)
    if context.awscontainer is None:
        raise click.ClickException("awscontainer がこの stage に設定されていません。")
    if skip_check_existing:
        apply_skip_check_existing(context)
    context.awscontainer.promote_commit_hash = commit_hash
    _deploy_pipeline(context, openpath=openpath, skip_frontend=skip_frontend)


def _get_deploy_url(context: Context) -> str | None:
    """デプロイ後に表示する URL を決定する。

    CloudFront がある場合はそのドメイン（カスタム or 自動生成）を優先し、
    なければ API Gateway の wsgi エンドポイントを返す。
    """
    # CloudFront ドメインを優先
    for _name, cf_ctx in context.cloudfront.items():
        if cf_ctx.domain:
            return f"https://{cf_ctx.domain}"
        cf = CloudFront(cf_ctx)
        if cf.stack.output:
            domain = cf.stack.output.get("DistributionDomainName")
            if domain:
                return f"https://{domain}"

    # フォールバック: API Gateway
    if context.awscontainer:
        ac = AwsContainer(context.awscontainer)
        endpoint = ac.endpoints.get("wsgi")
        if endpoint:
            return endpoint
    return None
