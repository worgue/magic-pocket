from pprint import pprint

import click

from pocket.context import Context
from pocket.utils import echo, mask_secret_values
from pocket_cli.cli.resource_helper import require_configured
from pocket_cli.cli.store_url_helper import run_store_url
from pocket_cli.cli.url_helper import run_get_url
from pocket_cli.resources.neon import (
    EnsureUrlInfo,
    Neon,
    ensure_url_for_context_with_info,
)


@click.group()
def neon():
    pass


def get_neon_resource(stage):
    context = Context.from_toml(stage=stage)
    return Neon(
        context=require_configured(
            context.neon, "neon is not configured for this stage"
        )
    )


@neon.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def context(stage):
    neon = get_neon_resource(stage)
    pprint(mask_secret_values(neon.context.model_dump()))


@neon.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def create(stage):
    neon = get_neon_resource(stage)
    neon.create()
    echo.success("New branch was created")


@neon.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def reset_database(stage):
    neon = get_neon_resource(stage)
    neon.reset_database()
    echo.success("Reset database")


@neon.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--base-stage", required=True, help="分岐元の stage")
def branch_out(stage, base_stage):
    neon = get_neon_resource(stage)
    if neon.branch:
        raise Exception("Branch already exists")
    base_neon = get_neon_resource(base_stage)
    if not base_neon.working:
        raise Exception("Base stage is not working")
    if not base_neon.branch:
        raise RuntimeError("base stage branch is not resolved")
    neon.create_branch(base_neon.branch)
    echo.success("New branch was created")


@neon.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="確認プロンプトをスキップ"
)
def delete(stage, yes):
    """stage の Neon ブランチを削除する。

    root branch は Neon 仕様で branch 単位の削除ができない (422: cannot delete the
    root branch) ため、project 内に他 branch が無ければ再確認のうえ project ごと
    削除する。他 branch が同居している場合は巻き添えになるためエラーで中断する。
    """
    if not yes:
        click.confirm(
            "stage '%s' の Neon ブランチを削除しますか？(データは失われます)" % stage,
            abort=True,
        )
    neon = get_neon_resource(stage)
    if neon.branch is None:
        raise click.ClickException(
            "Neon branch '%s' が見つかりません" % neon.context.branch_name
        )
    plan = neon.destroy_plan()
    if plan == "blocked":
        raise click.ClickException(
            "branch '%s' は root branch のため単体削除できません (Neon 仕様)。"
            "project '%s' には他の branch が残っているため project 削除も行いません。"
            "他の branch を先に削除してください。"
            % (neon.context.branch_name, neon.context.project_name)
        )
    if plan == "project":
        if not yes:
            click.confirm(
                "branch '%s' は root branch のため、project '%s' を丸ごと削除します。"
                "よろしいですか？"
                % (neon.context.branch_name, neon.context.project_name),
                abort=True,
            )
        neon.delete_project()
        echo.success("Project '%s' was deleted." % neon.context.project_name)
        return
    neon.delete_branch()
    echo.success("Branch was deleted successfully.")


def _echo_store_url_resolution(stage: str, info: EnsureUrlInfo) -> None:
    """store-url が焼く接続先の解決結果を表示する。

    意図と異なる branch へ焼いても「保存しました」だけでは気づけず、アプリが
    空 DB / 別 DB に接続して初めて発覚する (利用プロジェクトで実際に発生)。
    store-url は書き込む前に branch / endpoint を Neon に解決済みなので、
    既に手元にある情報を見せて pocket.toml の意図と目視照合できるようにする。
    """
    echo.info(
        "stage=%s → project=%s → branch=%s → endpoint=%s"
        % (stage, info.project_name, info.branch_name, info.endpoint_host)
    )
    if info.branch_created:
        echo.warning(
            "branch '%s' はこの実行で新規作成されました (空の branch)。既存データ"
            "へ接続するつもりなら、pocket.toml の [<stage>.neon] branch_name が"
            "意図した branch を指しているか確認してください。" % info.branch_name
        )


@neon.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option(
    "--key", default=None, help="保存先 user secret のキー (複数候補時に必須)"
)
@click.option("--force", is_flag=True, help="既存 secret があっても上書きする")
def store_url(stage, key, force):
    """branch/role/db を ensure し DATABASE_URL を stored user secret に保存する。

    provisioning="command" で deploy を Neon credential なしにするための provisioning
    ステップ。Neon の URL は reveal_password 方式で冪等なので何度実行しても同じ値。
    """

    def ensure_and_compute_url(context):
        if not context.neon:
            raise click.ClickException(
                "neon が pocket.toml に宣言されていません (store-url 不可)"
            )
        # ensure + URL 算出は runtime package の共有ヘルパに一本化 (公開 API と同一経路)
        info = ensure_url_for_context_with_info(context.neon)
        _echo_store_url_resolution(stage, info)
        return info.url

    run_store_url(
        stage=stage,
        secret_type="neon_database_url",  # noqa: S106 (secret type 名であって credential ではない)
        db_label="Neon",
        key=key,
        force=force,
        ensure_and_compute_url=ensure_and_compute_url,
    )


@neon.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option(
    "--live",
    is_flag=True,
    help="stored を見ず provider API で live 算出する (Neon は reveal 方式で冪等)",
)
def url(stage, live):
    """接続 URL を stdout に出力する (default: stored-first / --live: provider API)。

    移行ツール等が `$(pocket resource neon url --stage <s>)` で食える純 URL のみを
    stdout に出す (診断は stderr)。dual-declaration 下では source(Neon) の解決に使う。
    """

    def live_url(context):
        if not context.neon:
            raise click.ClickException(
                "neon が pocket.toml に宣言されていません (live 算出不可)"
            )
        return Neon(context.neon).database_url

    run_get_url(
        stage=stage,
        secret_type="neon_database_url",  # noqa: S106 (secret type 名であって credential ではない)
        db_label="Neon",
        live_url=live_url,
        live=live,
    )


@neon.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
def status(stage):
    neon = get_neon_resource(stage)
    if neon.project:
        echo.success("Project found")
    else:
        echo.warning("Project not found")
        return
    if neon.branch:
        echo.success("Branch found")
    else:
        echo.warning("Branch not found")
        return
    if neon.database:
        echo.success("Database found")
    else:
        echo.warning("Database not found")
        return
    if neon.endpoint:
        echo.success("Endpoint found: %s" % neon.endpoint.host)
    else:
        echo.warning("Endpoint not found")
    if neon.role:
        echo.success("Role found: %s" % neon.context.role_name)
    else:
        echo.warning("Role not found")
    if neon.role and neon.endpoint:
        # password は表示しない (status は状態確認用途。session log 等に secret を
        # 残さない)。フル URL は url コマンドが担う。
        echo.success("Database url: %s" % neon.masked_database_url)
        echo.info(
            "接続 URL (password 込み) は "
            "`pocket resource neon url --stage %s` を使用してください。" % stage
        )
