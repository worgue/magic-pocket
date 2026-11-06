"""`pocket static ...`: object_hash route の publish / adopt / purge / status。"""

from __future__ import annotations

import json
from pathlib import Path

import click

from pocket.context import Context
from pocket.utils import echo, get_toml_path
from pocket_cli.cli.resource_helper import require_configured
from pocket_cli.static_publish import StaticPublisher, StaticPublishError, status_report


@click.group()
def static():
    """内容 hash の immutable key で配信する静的ファイル (object_hash) の操作。"""


@static.group()
def hash():
    """map (path → 内容 hash) の操作。"""


def _stage_option(f):
    return click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)(f)


def _target_options(f):
    f = click.option(
        "--name", default=None, help="対象の cloudfront 名 (省略時は全部)"
    )(f)
    f = click.option(
        "--route",
        "route_ref",
        default=None,
        help="対象 route の ref か path_pattern (省略時は object_hash の全 route)",
    )(f)
    return f


def _jobs_option(f):
    return click.option(
        "--jobs", default=8, show_default=True, help="hash / upload の並列数"
    )(f)


def get_publishers(
    stage: str, name: str | None, route_ref: str | None, jobs: int = 8
) -> list[StaticPublisher]:
    context = Context.from_toml(stage=stage)
    require_configured(
        context.cloudfront, "cloudfront is not configured for this stage"
    )
    base_dir = Path(get_toml_path()).parent
    publishers: list[StaticPublisher] = []
    for cf_name, cf_ctx in context.cloudfront.items():
        if name is not None and cf_name != name:
            continue
        for route in cf_ctx.routes:
            if not route.is_object_hash:
                continue
            if route_ref is not None and route_ref not in (
                route.ref,
                route.path_pattern,
            ):
                continue
            publishers.append(
                StaticPublisher(
                    route,
                    bucket=cf_ctx.bucket_name,
                    region=cf_ctx.s3_region,
                    base_dir=base_dir,
                    jobs=jobs,
                )
            )
    if name is not None and name not in context.cloudfront:
        raise click.ClickException("cloudfront '%s' is not configured" % name)
    if not publishers:
        raise click.ClickException(
            'versioning = "object_hash" の route がありません'
            + (" (route %s)" % route_ref if route_ref else "")
        )
    return publishers


def _run(publishers: list[StaticPublisher], **kwargs) -> None:
    try:
        for publisher in publishers:
            publisher.publish(**kwargs)
    except StaticPublishError as e:
        raise click.ClickException(str(e)) from e


@static.command()
@_stage_option
@_target_options
@_jobs_option
@click.option(
    "--hash-generate",
    is_flag=True,
    default=False,
    help="ローカルから map を作り直してから publish する (dev 向け。map は commit)",
)
@click.option(
    "--allow-dirty",
    is_flag=True,
    default=False,
    help="map が未 commit / 作業ツリーと差があっても publish する",
)
@click.option(
    "--verify-existing",
    is_flag=True,
    default=False,
    help="S3 に既にある key も HEAD で内容を照合する (既定は hash 付き名は skip)",
)
def publish(stage, name, route_ref, jobs, hash_generate, allow_dirty, verify_existing):
    """sources を hash し、無い key だけ上げて release を切り替える。

    既定は commit 済みの map と照合する (prod 向け)。hash のずれ・map に無いファイル・
    map にあってローカルにも S3 にも無い path はエラーで止まる。途中で止まっても
    再実行で続きから進む (上げ終わった key は skip)。
    """
    _run(
        get_publishers(stage, name, route_ref, jobs),
        hash_generate=hash_generate,
        allow_dirty=allow_dirty,
        verify_existing=verify_existing,
    )


@static.command()
@_stage_option
@_target_options
@_jobs_option
@click.option("--hash-generate", is_flag=True, default=False, help="publish と同じ")
@click.option("--allow-dirty", is_flag=True, default=False, help="publish と同じ")
def adopt(stage, name, route_ref, jobs, hash_generate, allow_dirty):
    """既存の平置き key (同じ prefix の path そのまま) から hashed key を copy で作る。

    内容が一致する (ETag = MD5) 平置き key があれば CopyObject、無ければ通常の upload。
    deploy_hash / content_hash からの移行で、同じ内容を host から上げ直さない publish。
    """
    _run(
        get_publishers(stage, name, route_ref, jobs),
        hash_generate=hash_generate,
        allow_dirty=allow_dirty,
        adopt=True,
    )


@hash.command("generate")
@_stage_option
@_target_options
@_jobs_option
def hash_generate(stage, name, route_ref, jobs):
    """ローカルの sources を hash して map を書く (upload はしない)。"""
    try:
        for publisher in get_publishers(stage, name, route_ref, jobs):
            files = publisher.enumerate_sources()
            publisher.hash_files(files)
            static_map = publisher.build_map(files)
            path = publisher.write_map(static_map)
            echo.success(
                "[%s] %s を書きました (%d 件, release %s)"
                % (publisher.route_label, path, len(files), static_map.release_id)
            )
    except StaticPublishError as e:
        raise click.ClickException(str(e)) from e


@static.command()
@_stage_option
@_target_options
@click.option("--dry-run", is_flag=True, default=False, help="削除せず件数だけ表示")
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="確認プロンプトをスキップ"
)
def purge(stage, name, route_ref, dry_run, yes):
    """release が参照する object を全部消す (current と release 表は残す)。

    消してから次の publish までの間、その route の URL は 404 になる (配信を止める
    操作)。purge → publish で同じ内容は同じ key に戻るので URL は変わらない。
    """
    publishers = get_publishers(stage, name, route_ref)
    targets: list[tuple[StaticPublisher, list[str]]] = []
    for publisher in publishers:
        keys = publisher.purge(dry_run=True)
        echo.info("[%s] 削除対象 %d 件" % (publisher.route_label, len(keys)))
        if keys:
            targets.append((publisher, keys))
    total = sum(len(k) for _, k in targets)
    if not total:
        echo.success("削除対象はありません")
        return
    if dry_run:
        echo.info("--dry-run のため削除しません (%d 件)" % total)
        return
    echo.warning("消してから次の publish までの間、この route の URL は 404 になります")
    if not yes:
        click.confirm("上記 %d 件を削除しますか？" % total, abort=True)
    try:
        for publisher, _ in targets:
            deleted = publisher.purge()
            echo.info(
                "[%s] %d 件を削除しました" % (publisher.route_label, len(deleted))
            )
    except StaticPublishError as e:
        raise click.ClickException(str(e)) from e
    echo.success("%d 件を削除しました" % total)


@static.command()
@_stage_option
@_target_options
def status(stage, name, route_ref):
    """current の release と、ローカル map の release の一致を表示する。"""
    for publisher in get_publishers(stage, name, route_ref):
        report = status_report(publisher)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        if report.get("in_sync"):
            echo.success("[%s] current は map と一致しています" % publisher.route_label)
        elif report.get("current_release"):
            echo.warning(
                "[%s] current (%s) と map (%s) が違います"
                % (
                    publisher.route_label,
                    report["current_release"],
                    report.get("map_release", "無し"),
                )
            )
        else:
            echo.warning("[%s] まだ publish されていません" % publisher.route_label)
