import json

import click

from pocket.context import Context
from pocket.utils import echo, mask_secret_values
from pocket_cli.cli.resource_helper import require_configured
from pocket_cli.resources.cloudfront import CloudFront


@click.group()
def cloudfront():
    pass


def get_cloudfront_resources(stage, name=None):
    context = Context.from_toml(stage=stage)
    require_configured(
        context.cloudfront, "cloudfront is not configured for this stage"
    )
    if name:
        if name not in context.cloudfront:
            raise click.ClickException("cloudfront '%s' is not configured" % name)
        return [CloudFront(context.cloudfront[name])]
    return [CloudFront(cf_ctx) for cf_ctx in context.cloudfront.values()]


@cloudfront.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--name", default=None)
def yaml(stage, name):
    for cf in get_cloudfront_resources(stage, name):
        echo.info("[%s]" % cf.context.name)
        print(cf.stack.yaml)


@cloudfront.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--name", default=None)
def yaml_diff(stage, name):
    for cf in get_cloudfront_resources(stage, name):
        echo.info("[%s]" % cf.context.name)
        print(cf.stack.yaml_diff.to_json(indent=2))


@cloudfront.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--name", default=None)
def context(stage, name):
    for cf in get_cloudfront_resources(stage, name):
        echo.info("[%s]" % cf.context.name)
        dump = mask_secret_values(cf.context.model_dump(mode="json"))
        print(json.dumps(dump, indent=2))


@cloudfront.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--name", default=None)
def create(stage, name):
    for cf in get_cloudfront_resources(stage, name):
        echo.info("[%s]" % cf.context.name)
        cf.create()
    echo.success("cloudfront store was created")


@cloudfront.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--name", default=None)
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="確認プロンプトをスキップ"
)
def destroy(stage, name, yes):
    if not yes:
        click.confirm(
            "stage '%s' の CloudFront distribution を削除しますか？" % stage,
            abort=True,
        )
    for cf in get_cloudfront_resources(stage, name):
        echo.info("[%s]" % cf.context.name)
        cf.delete()
    echo.success("cloudfront store was deleted successfully.")


@cloudfront.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--name", default=None)
def update(stage, name):
    for cf in get_cloudfront_resources(stage, name):
        echo.info("[%s]" % cf.context.name)
        if cf.status == "NOEXIST":
            echo.warning("CloudFront resource has not created yet.")
            continue
        if cf.status == "FAILED":
            echo.danger(
                "CloudFront resource creation has failed. Please check console."
            )
            continue
        if cf.status == "PROGRESS":
            echo.warning("CloudFront is updating. Please wait.")
            continue
        cf.update()


@cloudfront.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--name", default=None)
@click.option("--skip-build", is_flag=True, default=False)
def upload(stage, name, skip_build):
    for cf in get_cloudfront_resources(stage, name):
        if not cf.context.uploadable_routes:
            echo.info("[%s] アップロード対象のルートがありません" % cf.context.name)
            continue
        echo.info("[%s] アップロード開始" % cf.context.name)
        cf.upload(skip_build=skip_build)
    echo.success("アップロード完了")


@cloudfront.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--name", default=None)
@click.option(
    "--route",
    "path_pattern",
    default=None,
    help="対象 route の path_pattern (省略時は build / upload_dir を持つ全 route)",
)
@click.option("--dry-run", is_flag=True, default=False, help="削除せず一覧だけ表示")
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="確認プロンプトをスキップ"
)
def prune(stage, name, path_pattern, dry_run, yes):
    """ローカルの成果物に無い S3 key (旧版) を削除する。

    delete_stale = false (content_hash の既定) で残した旧版の掃除用。ビルドは
    実行しないので、upload_dir を最新のビルドにしてから実行する。
    """
    targets = _collect_stale_keys(stage, name, path_pattern)
    total = sum(len(keys) for _, keys in targets)
    if not total:
        echo.success("削除対象の旧版はありません")
        return
    if dry_run:
        echo.info("--dry-run のため削除しません (%d 件)" % total)
        return
    if not yes:
        click.confirm("上記 %d 件を削除しますか？" % total, abort=True)
    for cf, keys in targets:
        cf.delete_objects(keys)
    echo.success("%d 件を削除しました" % total)


def _collect_stale_keys(
    stage, name, path_pattern
) -> list[tuple[CloudFront, list[str]]]:
    """route ごとの旧版 key を表示しつつ集める。

    --route が何にも当たらなければエラー。
    """
    targets: list[tuple[CloudFront, list[str]]] = []
    matched = False
    for cf in get_cloudfront_resources(stage, name):
        for route in cf.context.uploadable_routes:
            if path_pattern is not None and route.path_pattern != path_pattern:
                continue
            matched = True
            keys = cf.stale_keys(route)
            echo.info(
                "[%s] %s: 旧版 %d 件"
                % (cf.context.name, route.path_pattern or "/*", len(keys))
            )
            for key in keys:
                echo.log("  s3://%s/%s" % (cf.context.bucket_name, key))
            if keys:
                targets.append((cf, keys))
    if path_pattern is not None and not matched:
        raise click.ClickException(
            "route '%s' はアップロード対象 (build / upload_dir を持つ route)"
            " にありません" % path_pattern
        )
    return targets


@cloudfront.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--name", default=None)
def status(stage, name):
    for cf in get_cloudfront_resources(stage, name):
        echo.info("[%s]" % cf.context.name)
        if cf.status == "COMPLETED":
            echo.success("COMPLETED")
        else:
            print(cf.status)
