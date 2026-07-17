from pprint import pprint

import click

from pocket.context import Context
from pocket.utils import echo
from pocket_cli.cli.resource_helper import require_configured
from pocket_cli.cli.store_url_helper import run_store_url
from pocket_cli.cli.url_helper import run_get_url
from pocket_cli.resources.tidb import TiDb


@click.group()
def tidb():
    pass


def get_tidb_resource(stage):
    context = Context.from_toml(stage=stage)
    return TiDb(
        context=require_configured(
            context.tidb, "tidb is not configured for this stage"
        )
    )


@tidb.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def context(stage):
    resource = get_tidb_resource(stage)
    pprint(resource.context.model_dump())


@tidb.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def create(stage):
    resource = get_tidb_resource(stage)
    resource.create()
    echo.success("TiDB cluster and database created")


@tidb.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def reset_database(stage):
    resource = get_tidb_resource(stage)
    resource.reset_database()
    echo.success("Reset database")


@tidb.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="確認プロンプトをスキップ"
)
def delete(stage, yes):
    if not yes:
        click.confirm(
            "stage '%s' の TiDB クラスタを削除しますか？(データは失われます)" % stage,
            abort=True,
        )
    resource = get_tidb_resource(stage)
    resource.delete_cluster()
    echo.success("Cluster was deleted successfully.")


@tidb.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option(
    "--key", default=None, help="保存先 user secret のキー (複数候補時に必須)"
)
@click.option("--force", is_flag=True, help="既存 secret があっても上書きする")
def store_url(stage, key, force):
    """cluster/db を ensure し DATABASE_URL を stored user secret に保存する。

    provisioning="command" で deploy を TiDB credential なしにするための provisioning
    ステップ。注意: TiDB serverless は password reveal API が無いため、本コマンドは
    実行のたびに root password をローテーションする (既存 secret は --force が必要。
    実行後は consumer の redeploy が前提)。
    """

    def ensure_and_compute_url(context):
        if not context.tidb:
            raise click.ClickException(
                "tidb が pocket.toml に宣言されていません (store-url 不可)"
            )
        # TiDB は password reveal が無く ensure/url 算出で password を reset するため、
        # 同一インスタンスを使い回して password を整合させる (fresh instance にしない)。
        resource = TiDb(context.tidb)
        resource.create()
        return resource.database_url

    run_store_url(
        stage=stage,
        secret_type="tidb_database_url",  # noqa: S106 (secret type 名であって credential ではない)
        db_label="TiDB",
        key=key,
        force=force,
        ensure_and_compute_url=ensure_and_compute_url,
    )


@tidb.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option(
    "--live",
    is_flag=True,
    help=(
        "stored user secret を見ず provider API で live 算出する。"
        "注意: TiDB は reveal API が無いため root password を rotate する "
        "(consumer の redeploy が前提)"
    ),
)
def url(stage, live):
    """接続 URL を stdout に出力する (default: stored-first / --live: provider API)。

    移行ツール等が `$(pocket resource tidb url --stage <s>)` で食える純 URL のみを
    stdout に出す (診断は stderr)。dual-declaration 下では target(TiDB) の解決に使う。
    default の stored-first は副作用が無く consumer が使う URL と一致する。--live は
    root password を rotate する点に注意。
    """

    def live_url(context):
        if not context.tidb:
            raise click.ClickException(
                "tidb が pocket.toml に宣言されていません (live 算出不可)"
            )
        return TiDb(context.tidb).database_url

    run_get_url(
        stage=stage,
        secret_type="tidb_database_url",  # noqa: S106 (secret type 名であって credential ではない)
        db_label="TiDB",
        live_url=live_url,
        live=live,
        live_rotates_credentials=True,
    )


@tidb.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def status(stage):
    resource = get_tidb_resource(stage)
    if resource.project:
        echo.success("Project found")
    else:
        echo.warning("Project not found")
        return
    if resource.cluster:
        echo.success(
            "Cluster found: %s (%s)" % (resource.cluster.name, resource.cluster.status)
        )
    else:
        echo.warning("Cluster not found")
        return
    if resource.cluster.status == "ACTIVE":
        # database_url は password reveal API が無い TiDB では root password を
        # rotate してしまうため、status では endpoint のみ表示する
        echo.success(
            "Cluster endpoint: %s:%d (user: %s)"
            % (resource.cluster.host, resource.cluster.port, resource.cluster.user)
        )
        echo.info(
            "接続 URL は `pocket resource tidb url --stage %s` を使用してください。"
            % stage
        )
    else:
        echo.warning("Cluster status: %s" % resource.cluster.status)
