"""CloudFront.upload の差分アップロードと invalidation 範囲の検証。

数千ファイル規模の配信アセットを upload_dir に置いても deploy が伸びないよう、
内容が一致するオブジェクトは skip する。誤って skip すると「以後どの deploy でも
更新されないファイル」が生まれるため、skip 条件 (ETag が素の MD5 で一致) の
境界を重点的に固定する。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest import mock

import pytest
from pocket_cli.resources.cloudfront import CloudFront

from pocket.context import CloudFrontContext, RouteContext


def _make_cf(routes: list[RouteContext]) -> CloudFront:
    ctx = CloudFrontContext(
        name="web",
        region="ap-northeast-1",
        s3_region="ap-northeast-1",
        stage="dev",
        slug="dev-testprj-web",
        bucket_name="dev-testprj-bucket",
        resource_prefix="dev-testprj-",
        routes=routes,
    )
    with mock.patch("boto3.client"):
        return CloudFront(ctx)


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()


@pytest.fixture
def upload_dir(tmp_path: Path) -> Path:
    d = tmp_path / "assets"
    d.mkdir()
    (d / "a.svg").write_text("<svg>a</svg>")
    (d / "b.svg").write_text("<svg>b</svg>")
    return d


def _route(upload_dir: Path) -> RouteContext:
    return RouteContext(
        path_pattern="/twemoji/*",
        origin_path="",
        upload_dir=str(upload_dir),
    )


def test_unchanged_files_are_skipped(upload_dir: Path):
    """ETag (素の MD5) が一致するファイルは upload_file を呼ばない"""
    cf = _make_cf([_route(upload_dir)])
    existing = {
        "twemoji/a.svg": _md5(upload_dir / "a.svg"),
        "twemoji/b.svg": _md5(upload_dir / "b.svg"),
    }
    with mock.patch.object(cf, "_list_objects", return_value=existing):
        changed = cf._upload_route(cf.context.routes[0])

    cf.s3_client.upload_file.assert_not_called()
    cf.s3_client.delete_object.assert_not_called()
    assert changed is False


def test_changed_and_new_files_are_uploaded(upload_dir: Path):
    """内容が変わったファイルと未アップロードのファイルだけ上げ直す"""
    cf = _make_cf([_route(upload_dir)])
    existing = {
        "twemoji/a.svg": _md5(upload_dir / "a.svg"),
        "twemoji/b.svg": hashlib.md5(b"old", usedforsecurity=False).hexdigest(),
    }
    with mock.patch.object(cf, "_list_objects", return_value=existing):
        changed = cf._upload_route(cf.context.routes[0])

    uploaded = {c.args[2] for c in cf.s3_client.upload_file.call_args_list}
    assert uploaded == {"twemoji/b.svg"}
    assert changed is True


def test_multipart_etag_never_skips(upload_dir: Path):
    """multipart の ETag (`<md5>-<n>`) は内容を断定できないので必ず上げ直す

    サイズ比較で代替すると、同サイズ別内容のファイルが恒久的に更新されなく
    なる。ETag が MD5 でないものは一律「変更あり」とする (回帰テスト)。
    """
    cf = _make_cf([_route(upload_dir)])
    existing = {
        "twemoji/a.svg": _md5(upload_dir / "a.svg") + "-3",
        "twemoji/b.svg": _md5(upload_dir / "b.svg"),
    }
    with mock.patch.object(cf, "_list_objects", return_value=existing):
        cf._upload_route(cf.context.routes[0])

    uploaded = {c.args[2] for c in cf.s3_client.upload_file.call_args_list}
    assert uploaded == {"twemoji/a.svg"}


def test_stale_objects_are_deleted_and_count_as_change(upload_dir: Path):
    """ローカルに無い key は削除され、削除だけでも変更ありとして扱う"""
    cf = _make_cf([_route(upload_dir)])
    existing = {
        "twemoji/a.svg": _md5(upload_dir / "a.svg"),
        "twemoji/b.svg": _md5(upload_dir / "b.svg"),
        "twemoji/gone.svg": "whatever",
    }
    with mock.patch.object(cf, "_list_objects", return_value=existing):
        changed = cf._upload_route(cf.context.routes[0])

    cf.s3_client.upload_file.assert_not_called()
    cf.s3_client.delete_object.assert_called_once_with(
        Bucket="dev-testprj-bucket", Key="twemoji/gone.svg"
    )
    assert changed is True


def test_invalidate_limits_paths_to_changed_routes(upload_dir: Path):
    """invalidation は変更があった route の path_pattern に限定される"""
    changed = RouteContext(path_pattern="/twemoji/*", upload_dir=str(upload_dir))
    cf = _make_cf([changed])
    with mock.patch.object(
        CloudFront, "distribution_id", new_callable=mock.PropertyMock
    ) as dist_id:
        dist_id.return_value = "E123"
        cf._invalidate([changed])

    batch = cf.cf_client.create_invalidation.call_args.kwargs["InvalidationBatch"]
    assert batch["Paths"]["Items"] == ["/twemoji/*"]
    assert batch["Paths"]["Quantity"] == 1


def test_invalidate_uses_wildcard_for_default_route(upload_dir: Path):
    """default route (path_pattern 空) は従来どおり /* を invalidate する"""
    default = RouteContext(is_default=True, origin_path="/app", upload_dir="dist")
    cf = _make_cf([default])
    with mock.patch.object(
        CloudFront, "distribution_id", new_callable=mock.PropertyMock
    ) as dist_id:
        dist_id.return_value = "E123"
        cf._invalidate([default])

    batch = cf.cf_client.create_invalidation.call_args.kwargs["InvalidationBatch"]
    assert batch["Paths"]["Items"] == ["/*"]


def test_invalidate_skipped_when_nothing_changed():
    """変更が無ければ invalidation を出さない (配信専用 route の巻き添え防止)"""
    cf = _make_cf([])
    cf._invalidate([])
    cf.cf_client.create_invalidation.assert_not_called()


def test_upload_invalidates_only_changed_routes(upload_dir: Path):
    """upload() は変更のあった route だけを _invalidate へ渡す"""
    changed = RouteContext(path_pattern="/twemoji/*", upload_dir=str(upload_dir))
    unchanged = RouteContext(path_pattern="/static/*", upload_dir=str(upload_dir))
    cf = _make_cf([changed, unchanged])
    with (
        mock.patch.object(cf, "_upload_route", side_effect=[True, False]),
        mock.patch.object(cf, "_invalidate") as invalidate,
    ):
        cf.upload()

    assert invalidate.call_args.args[0] == [changed]
