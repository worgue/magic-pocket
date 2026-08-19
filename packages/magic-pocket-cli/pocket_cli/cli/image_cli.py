"""deploy 済みコンテナイメージの参照情報を出力する CLI。

外部ツール (pocket と併走する deploy 系) が pocket のビルドしたイメージを
参照する際、pocket.toml を読んだ正確な値をスクリプトから取得できるようにする
(`pocket.naming.ecr_repo_name` の「toml を読む版」)。stdout には値のみを出し、
`$(pocket resource image uri --stage <s>)` の形で食えるようにする (診断は stderr)。
"""

import click

from pocket_cli.cli.container_cli import _container_option, get_container_resource


@click.group()
def image():
    pass


@image.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@_container_option
def repo(stage, container_name):
    """ECR repository 名を stdout に出力する (ecr_name 上書きを含め toml 準拠)。"""
    c = get_container_resource(stage, container_name)
    click.echo(c.context.ecr_name)


@image.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@_container_option
def uri(stage, container_name):
    """deploy 済み実イメージの URI ({repo_uri}@{digest}) を stdout に出力する。

    `:{stage}` タグが現在指している image を digest で固定した URI を返すため、
    以後の deploy でタグが動いても参照が壊れない。
    """
    c = get_container_resource(stage, container_name)
    ecr = c.ecr
    if not ecr.uri:
        raise click.ClickException(
            "ECR repository '%s' が見つかりません。先に `pocket deploy` を"
            "実行してください。" % c.context.ecr_name
        )
    digest = ecr.image_detail.image_digest
    if not digest:
        raise click.ClickException(
            "tag ':%s' のイメージが '%s' に見つかりません。deploy 済みか確認して"
            "ください。" % (stage, c.context.ecr_name)
        )
    click.echo("%s@%s" % (ecr.uri, digest))
