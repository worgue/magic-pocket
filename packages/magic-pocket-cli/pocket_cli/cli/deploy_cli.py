import inspect
import webbrowser

import boto3
import click

from pocket.context import Context, deploy_hash_report
from pocket.utils import echo
from pocket_cli.cli import interaction
from pocket_cli.cli.removed_flags import removed_skip_check_existing
from pocket_cli.mediator import Mediator
from pocket_cli.resources.aws.state import StateStore, create_state_store
from pocket_cli.resources.backup import Backup
from pocket_cli.resources.cloudfront import CloudFront
from pocket_cli.resources.cloudfront_acm import CloudFrontAcm
from pocket_cli.resources.cloudfront_keys import CloudFrontKeys
from pocket_cli.resources.cloudfront_waf import CloudFrontWaf
from pocket_cli.resources.container import Container
from pocket_cli.resources.dsql import Dsql
from pocket_cli.resources.neon import Neon
from pocket_cli.resources.rds import Rds
from pocket_cli.resources.s3 import S3
from pocket_cli.resources.tidb import TiDb
from pocket_cli.resources.upstash import Upstash
from pocket_cli.resources.vpc import Vpc


def _append_infra_resources(resources, context: Context, state_bucket: str):
    """VPC / RDS / CloudFrontKeys / Container をまとめて追加"""
    managed_vpc_names: set[str] = set()
    for c_ctx in context.container.values():
        if c_ctx.vpc and c_ctx.vpc.manage and c_ctx.vpc.name not in managed_vpc_names:
            managed_vpc_names.add(c_ctx.vpc.name)
            resources.append(Vpc(c_ctx.vpc))
    if context.dsql:
        resources.append(Dsql(context.dsql))
    if context.rds:
        resources.append(Rds(context.rds))
    # declared でなくても組み込む (0.27 以前の旧形式 plan の掃除が
    # ensure_post_deploy_state で走るため)
    if context.backup and (context.backup.declared or context.backup.legacy_plan_names):
        resources.append(
            Backup(context.backup, dsql_context=context.dsql, rds_context=context.rds)
        )
    for _name, cf_ctx in context.cloudfront.items():
        if cf_ctx.signing_key:
            resources.append(CloudFrontKeys(cf_ctx))
    for c_name in sorted(context.container):
        resources.append(
            Container(
                context.container[c_name],
                state_bucket=state_bucket,
                rds_context=context.rds,
                dsql_context=context.dsql,
                scheduler_context=context.scheduler.get(c_name),
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
    # provisioning="command" の DB は deploy が管理しない (credential 不要)。
    # provisioning は `pocket <db> store-url` に一任し、deploy は stored-read のみ。
    if context.neon and context.neon.provisioning != "command":
        resources.append(Neon(context.neon))
    if context.tidb and context.tidb.provisioning != "command":
        resources.append(TiDb(context.tidb))
    if context.upstash and context.upstash.provisioning != "command":
        resources.append(Upstash(context.upstash))
    if context.s3:
        resources.append(S3(context.s3, cloudfront_contexts=context.cloudfront))
    _append_infra_resources(resources, context, state_bucket)
    for _name, cf_ctx in context.cloudfront.items():
        resources.append(CloudFront(cf_ctx))
    return resources


def _create_state_store(context: Context) -> StateStore:
    return create_state_store(context)


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


def _deploy_resource(resource, mediator: Mediator, state_store: StateStore):
    target_name = resource.__class__.__name__
    # template hash に影響する入力 (secret 値等) を status 判定前に読み込む。
    # 空のまま hash を計算すると deploy 済み hash と一致せず、secret 焼き込み
    # 構成 (enable_origin_verify / signing_key) で毎回 REQUIRE_UPDATE になる
    prepare = getattr(resource, "prepare_deploy", None)
    if prepare is not None:
        prepare(mediator)
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
    elif resource.status == "FAILED":
        # 黙ってスキップすると deploy が exit 0 で成功表示になる
        raise RuntimeError(
            "%s が FAILED 状態です。AWS コンソール等で状態を確認して"
            "解消してから再実行してください。" % target_name
        )
    elif resource.status == "PROGRESS":
        raise RuntimeError(
            "%s の前回操作が進行中です。完了を待ってから再実行してください。"
            % target_name
        )
    else:
        echo.log("%s is already the latest version." % target_name)


def deploy_resources(context: Context, *, state_bucket: str = ""):
    state_store = _create_state_store(context)
    # state bucket は deploy_init_resources の前に作成済み
    # ここでは念のため再確認
    state_store.ensure_bucket()

    mediator = Mediator(context)
    resources = get_resources(context, state_bucket=state_bucket)
    for resource in resources:
        _deploy_resource(resource, mediator, state_store)
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


def build_image(context: Context, *, tag: str) -> list[str]:
    """全 container image を指定 tag で build & push する (deploy はしない)。

    build once 用。codebuild backend は source upload に state bucket を要するため、
    deploy と同様に先に state bucket を確保してから build する。
    戻り値は ecr_name:tag のリスト (container 名順)。
    """
    if not context.container:
        raise click.ClickException("container がこの stage に設定されていません。")
    state_store = _create_state_store(context)
    state_store.ensure_bucket()
    targets = []
    for c_name in sorted(context.container):
        c_ctx = context.container[c_name]
        echo.log("Building container '%s'..." % c_name)
        Container(c_ctx, state_bucket=state_store.bucket_name).build(tag)
        targets.append(f"{c_ctx.ecr_name}:{tag}")
    return targets


def cleanup_legacy_container_resources(context: Context):
    """0.29.0 以前の単数 [awscontainer] 由来の旧リソースを検出して削除する。

    旧 container stack ({slug}-container) は scheduler / SQS event source を
    持ったまま残ると旧コードの cron / queue 消費が動き続けるため、放置は
    実害がある。新 stack + cloudfront 切替が完了した deploy の後 (= 旧 stack が
    参照フリーになった後) に、確認プロンプト付きで削除する (-y で自動承認)。
    旧 ECR repo ({prefix}lambda) も、どの container も ecr_name で参照して
    いなければ削除する。冪等 (無ければ何もしない)。
    """
    if not context.container or not context.general:
        return
    slug = f"{context.stage}-{context.project_name}"
    region = context.general.region
    legacy_stack_name = f"{slug}-container"
    cfn = boto3.client("cloudformation", region_name=region)
    try:
        cfn.describe_stacks(StackName=legacy_stack_name)
        stack_exists = True
    except cfn.exceptions.ClientError:
        stack_exists = False
    if stack_exists:
        echo.warning(
            "旧形式の container stack '%s' が残っています (0.29.0 の "
            "multi-container 化で stack 名が {slug}-container-{name} に"
            "変わりました)。旧 stack の scheduler / SQS が動き続けるため、"
            "削除を推奨します。" % legacy_stack_name
        )
        if interaction.confirm(
            "旧 stack '%s' を削除しますか？" % legacy_stack_name, default=True
        ):
            cfn.delete_stack(StackName=legacy_stack_name)
            echo.log("旧 stack の削除を開始しました (完了待ちはしません)。")
    resource_prefix = context.general.prefix_template.format(
        stage=context.stage,
        project=context.project_name,
        namespace=context.general.namespace,
    )
    legacy_repo = f"{resource_prefix}lambda"
    if any(c.ecr_name == legacy_repo for c in context.container.values()):
        return
    ecr = boto3.client("ecr", region_name=region)
    try:
        ecr.describe_repositories(repositoryNames=[legacy_repo])
    except ecr.exceptions.RepositoryNotFoundException:
        return
    echo.warning(
        "旧形式の ECR repository '%s' が残っています (新しい repo 名は "
        "{prefix}{container}-lambda)。" % legacy_repo
    )
    if interaction.confirm(
        "旧 ECR repository '%s' を削除しますか？" % legacy_repo, default=True
    ):
        ecr.delete_repository(repositoryName=legacy_repo, force=True)
        echo.success("旧 ECR repository を削除しました。")


def _deploy_pipeline(context: Context, *, openpath=None, skip_frontend=False):
    """deploy / promote 共通のパイプライン本体。

    promote 時は各 container の promote_commit_hash が設定済みで、
    deploy_init 内の image build が retag に置き換わる以外は deploy と同一。
    """
    # DEPLOY_HASH の解決結果を deploy 時に 1 回可視化する (env 伝播漏れで
    # 黙って git short hash に落ちる footgun の早期発見用)。
    deploy_hash_message = deploy_hash_report(context)
    if deploy_hash_message:
        echo.info(deploy_hash_message)
    # CodeBuildがソースアップロードにstate bucketを必要とするため、先に作成
    state_store = _create_state_store(context)
    state_store.ensure_bucket()
    state_bucket = state_store.bucket_name
    deploy_init_resources(context, state_bucket=state_bucket)
    deploy_resources(context, state_bucket=state_bucket)
    cleanup_legacy_container_resources(context)
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
    "--yes", "-y", is_flag=True, default=False, help="確認プロンプトをスキップ"
)
@removed_skip_check_existing
def deploy(stage: str, openpath, skip_frontend, yes):
    from pocket_cli.cli.aws_auth import check_aws_credentials

    interaction.set_assume_yes(yes)
    check_aws_credentials()
    context = Context.from_toml(stage=stage)
    _deploy_pipeline(context, openpath=openpath, skip_frontend=skip_frontend)


@click.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--commit-hash", required=True, help="昇格する image の git commit hash")
@click.option("--openpath")
@click.option("--skip-frontend", is_flag=True, default=False)
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="確認プロンプトをスキップ"
)
@removed_skip_check_existing
def promote(stage: str, commit_hash, openpath, skip_frontend, yes):
    """build 済みの :<commit-hash> image へ stage を向けて deploy する (再ビルドなし)。

    `pocket django build` で push した image に :<stage> タグを移し、
    インフラ/Lambda を更新する。image build は行わない (build once の昇格)。
    """
    from pocket_cli.cli.aws_auth import check_aws_credentials

    interaction.set_assume_yes(yes)
    check_aws_credentials()
    context = Context.from_toml(stage=stage)
    if not context.container:
        raise click.ClickException("container がこの stage に設定されていません。")
    for c_ctx in context.container.values():
        c_ctx.promote_commit_hash = commit_hash
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

    # フォールバック: API Gateway (container 名順で最初に見つかった endpoint)
    for c_name in sorted(context.container):
        endpoints = Container(context.container[c_name]).endpoints
        if "wsgi" in endpoints:
            return endpoints["wsgi"]
        if endpoints:
            return next(iter(endpoints.values()))
    return None
