from pprint import pprint

import click

from pocket.context import Context
from pocket.resources.s3 import S3
from pocket.utils import echo


@click.group()
def s3():
    pass


def get_s3_resource(stage):
    context = Context.from_toml(stage=stage)
    if not context.s3:
        echo.danger("s3 is not configured for this stage")
        raise Exception("s3 is not configured for this stage")
    return S3(context=context.s3)


@s3.command()
@click.option("--stage", prompt=True)
def context(stage):
    storage = get_s3_resource(stage)
    pprint(storage.context.model_dump())


@s3.command()
@click.option("--stage", prompt=True)
def create(stage):
    storage = get_s3_resource(stage)
    if storage.exists():
        echo.warning(
            "S3 Bucket is already created by you.\n"
            "Presume this bucket is properly configured.\n"
            "Please check the bucket.",
        )
        return
    storage.ensure_exists()
    echo.success("Created: bucket %s" % storage.context.bucket_name)


@s3.command()
@click.option("--stage", prompt=True)
def update_public_dirs(stage):
    storage = get_s3_resource(stage)
    if not storage.exists():
        echo.warning("No bucket found.")
    storage.ensure_public_access_block()
    storage.ensure_policy()


@s3.command()
@click.option("--stage", prompt=True)
def status(stage):
    storage = get_s3_resource(stage)
    if storage.exists():
        echo.success("Storage found")
    else:
        echo.warning("Storage not found")
