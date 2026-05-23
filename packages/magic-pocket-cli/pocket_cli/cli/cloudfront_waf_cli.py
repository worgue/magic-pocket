import click

from pocket.context import Context
from pocket.utils import echo
from pocket_cli.resources.cloudfront_waf import CloudFrontWaf


@click.group()
def cloudfront_waf():
    pass


def get_cloudfront_waf_resources(stage, name=None):
    context = Context.from_toml(stage=stage)
    if not context.cloudfront:
        echo.danger("cloudfront is not configured for this stage")
        raise Exception("cloudfront is not configured for this stage")
    results = []
    for cf_name, cf_ctx in context.cloudfront.items():
        if cf_ctx.waf is None:
            continue
        if name and cf_name != name:
            continue
        results.append(CloudFrontWaf(cf_ctx))
    if name and not results:
        echo.danger("cloudfront_waf '%s' is not configured" % name)
        raise Exception("cloudfront_waf '%s' is not configured" % name)
    return results


@cloudfront_waf.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--name", default=None)
def yaml(stage, name):
    for waf in get_cloudfront_waf_resources(stage, name):
        echo.info("[%s]" % waf.context.name)
        print(waf.stack.yaml)


@cloudfront_waf.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--name", default=None)
def yaml_diff(stage, name):
    for waf in get_cloudfront_waf_resources(stage, name):
        echo.info("[%s]" % waf.context.name)
        print(waf.stack.yaml_diff.to_json(indent=2))


@cloudfront_waf.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--name", default=None)
def status(stage, name):
    for waf in get_cloudfront_waf_resources(stage, name):
        echo.info("[%s]" % waf.context.name)
        if waf.status == "COMPLETED":
            echo.success("COMPLETED")
        else:
            print(waf.status)


@cloudfront_waf.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--name", default=None)
def destroy(stage, name):
    for waf in get_cloudfront_waf_resources(stage, name):
        echo.info("[%s]" % waf.context.name)
        waf.delete()
    echo.success("cloudfront_waf was deleted successfully.")
