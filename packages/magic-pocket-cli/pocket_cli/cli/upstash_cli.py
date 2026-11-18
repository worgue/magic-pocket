import click

from pocket.context import Context
from pocket_cli.cli.resource_helper import require_configured
from pocket_cli.cli.store_url_helper import run_store_url
from pocket_cli.resources.upstash import Upstash


@click.group()
def upstash():
    pass


@upstash.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option(
    "--key", default=None, help="保存先 user secret のキー (複数候補時に必須)"
)
@click.option("--force", is_flag=True, help="既存 secret があっても上書きする")
def store_url(stage, key, force):
    """database を ensure し REDIS_URL を stored user secret に保存する。

    deploy は Upstash に触れないので、API key がある場所でこのコマンドを実行して
    database を用意する。Upstash の URL は database の password を読み出すだけで
    冪等なので、何度実行しても同じ値になる。
    """

    def ensure_and_compute_url(context):
        if not context.upstash:
            raise click.ClickException(
                "upstash が pocket.toml に宣言されていません (store-url 不可)"
            )
        resource = Upstash(context.upstash)
        resource.create()
        # ensure 後の状態を確実に反映するため fresh instance で URL を算出する。
        return Upstash(context.upstash).redis_url

    run_store_url(
        stage=stage,
        secret_type="upstash_redis_url",  # noqa: S106 (secret type 名であって credential ではない)
        db_label="Upstash",
        key=key,
        force=force,
        ensure_and_compute_url=ensure_and_compute_url,
    )


@upstash.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="確認プロンプトをスキップ"
)
def delete(stage, yes):
    """stage の Upstash database を削除する (destroy では消えない)。"""
    if not yes:
        click.confirm(
            "stage '%s' の Upstash database を削除しますか？(データは失われます)"
            % stage,
            abort=True,
        )
    context = Context.from_toml(stage=stage)
    Upstash(
        require_configured(context.upstash, "upstash is not configured for this stage")
    ).delete_database()
