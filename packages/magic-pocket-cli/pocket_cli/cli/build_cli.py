"""フレームワーク共通の build once コマンド。"""

import click

from pocket.context import Context, get_commit_hash, is_working_tree_dirty
from pocket.utils import echo
from pocket_cli.cli.aws_auth import check_aws_credentials
from pocket_cli.cli.deploy_cli import build_image


@click.command()
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option(
    "--allow-dirty",
    is_flag=True,
    default=False,
    help="working tree が dirty でも build する (ローカル検証用)",
)
def build(stage: str, allow_dirty: bool):
    """現在の作業ツリーを build し、git commit hash をタグにして ECR へ push する。

    deploy はしない (build once)。`pocket promote --commit-hash <sha>` で
    このイメージへ昇格する。タグは COMMIT_HASH 環境変数があればそれを、なければ
    `git rev-parse HEAD` を使う。

    commit hash = image 内容 の同一性が前提のため、working tree が dirty の場合は
    エラーになる (--allow-dirty で回避可能)。
    """
    check_aws_credentials()
    if not allow_dirty and is_working_tree_dirty():
        raise click.ClickException(
            "working tree に未コミットの変更があります。build once では"
            " commit hash と image 内容の一致が前提のため、commit してから"
            " build してください (--allow-dirty で回避できますが、その image の"
            " 昇格は推奨しません)。"
        )
    try:
        tag = get_commit_hash()
    except RuntimeError as e:
        raise click.ClickException(str(e)) from e
    context = Context.from_toml(stage=stage)
    targets = build_image(context, tag=tag)
    for target in targets:
        echo.success("built and pushed: %s" % target)
