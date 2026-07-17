import click

from pocket.context import Context
from pocket.utils import echo
from pocket_cli.cli.resource_helper import require_configured
from pocket_cli.resources.cloudfront_keys import CloudFrontKeys


@click.group()
def cloudfront_keys():
    pass


def get_cloudfront_keys_resources(stage, name=None):
    context = Context.from_toml(stage=stage)
    require_configured(
        context.cloudfront, "cloudfront is not configured for this stage"
    )
    results = []
    for cf_name, cf_ctx in context.cloudfront.items():
        if not cf_ctx.signing_key:
            continue
        if name and cf_name != name:
            continue
        results.append(CloudFrontKeys(cf_ctx))
    if name and not results:
        raise click.ClickException("cloudfront_keys '%s' is not configured" % name)
    return results


@cloudfront_keys.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--name", default=None)
def yaml(stage, name):
    for cfk in get_cloudfront_keys_resources(stage, name):
        echo.info("[%s]" % cfk.context.name)
        print(cfk.stack.yaml)


@cloudfront_keys.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--name", default=None)
def yaml_diff(stage, name):
    for cfk in get_cloudfront_keys_resources(stage, name):
        echo.info("[%s]" % cfk.context.name)
        print(cfk.stack.yaml_diff.to_json(indent=2))


@cloudfront_keys.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--name", default=None)
def status(stage, name):
    for cfk in get_cloudfront_keys_resources(stage, name):
        echo.info("[%s]" % cfk.context.name)
        if cfk.status == "COMPLETED":
            echo.success("COMPLETED")
        else:
            print(cfk.status)


@cloudfront_keys.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--name", default=None)
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="確認プロンプトをスキップ"
)
def destroy(stage, name, yes):
    if not yes:
        click.confirm(
            "stage '%s' の CloudFront signing key リソースを削除しますか？"
            "(署名済み URL が無効になります)" % stage,
            abort=True,
        )
    for cfk in get_cloudfront_keys_resources(stage, name):
        echo.info("[%s]" % cfk.context.name)
        cfk.delete()
    echo.success("cloudfront_keys was deleted successfully.")
