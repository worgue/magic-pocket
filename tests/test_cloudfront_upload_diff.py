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


def test_upload_raises_multipart_threshold(upload_dir: Path):
    """upload_file は multipart 閾値 128MB の TransferConfig 付きで呼ばれる

    boto3 既定 (8MB) のままだと 8MB 超のファイルの ETag が multipart 形式に
    なり、内容不変でも毎 deploy 再アップロード + route 全体の invalidation が
    走る (差分 skip が恒久的に効かない)。閾値以下を単一 PUT に保つことで
    ETag が素の MD5 になり _is_unchanged が機能する (回帰テスト)。
    """
    cf = _make_cf([_route(upload_dir)])
    with mock.patch.object(cf, "_list_objects", return_value={}):
        cf._upload_route(cf.context.routes[0])

    assert cf.s3_client.upload_file.call_count == 2
    for call in cf.s3_client.upload_file.call_args_list:
        config = call.kwargs["Config"]
        assert config.multipart_threshold == 128 * 1024 * 1024


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


def _content_hash_route(upload_dir: Path, **kwargs) -> RouteContext:
    return RouteContext(
        path_pattern="/v2/_app/immutable/*",
        upload_dir=str(upload_dir),
        versioning="content_hash",
        **kwargs,
    )


def test_content_hash_keeps_stale_objects_by_default(upload_dir: Path):
    """content_hash は今回の成果物に無い key を既定で消さない (KN2009)

    内容 hash 付きの名前は上書きで壊れないので消す必要が無く、消すと deploy 前
    から開いたままの画面の遅延ロード chunk や、旧 image へロールバックしたときの
    JS / CSS が 404 になる。
    """
    cf = _make_cf([_content_hash_route(upload_dir)])
    existing = {
        "v2/_app/immutable/a.svg": _md5(upload_dir / "a.svg"),
        "v2/_app/immutable/b.svg": _md5(upload_dir / "b.svg"),
        "v2/_app/immutable/old-chunk.js": "whatever",
    }
    with mock.patch.object(cf, "_list_objects", return_value=existing):
        changed = cf._upload_route(cf.context.routes[0])

    cf.s3_client.delete_object.assert_not_called()
    assert changed is False


def test_content_hash_delete_stale_true_deletes(upload_dir: Path):
    """content_hash でも delete_stale = true を明示すれば旧版を消す (掃除用)"""
    cf = _make_cf([_content_hash_route(upload_dir, delete_stale=True)])
    existing = {
        "v2/_app/immutable/a.svg": _md5(upload_dir / "a.svg"),
        "v2/_app/immutable/b.svg": _md5(upload_dir / "b.svg"),
        "v2/_app/immutable/old-chunk.js": "whatever",
    }
    with mock.patch.object(cf, "_list_objects", return_value=existing):
        changed = cf._upload_route(cf.context.routes[0])

    cf.s3_client.delete_object.assert_called_once_with(
        Bucket="dev-testprj-bucket", Key="v2/_app/immutable/old-chunk.js"
    )
    assert changed is True


def test_plain_route_delete_stale_false_keeps(upload_dir: Path):
    """versioning 無しの route でも delete_stale = false なら消さない"""
    cf = _make_cf(
        [
            RouteContext(
                path_pattern="/twemoji/*",
                upload_dir=str(upload_dir),
                delete_stale=False,
            )
        ]
    )
    existing = {"twemoji/gone.svg": "whatever"}
    with mock.patch.object(cf, "_list_objects", return_value=existing):
        cf._upload_route(cf.context.routes[0])

    cf.s3_client.delete_object.assert_not_called()


def test_content_hash_upload_sets_immutable_cache_control(upload_dir: Path):
    """content_hash のオブジェクトには長期 Cache-Control を付ける (KN2009)

    viewer 向けは CloudFront Function が付けるが、オブジェクト側に無いと edge は
    cache policy の DefaultTTL (1 日) で切れて origin へ戻る。max-age は Function と
    同じ versioned_max_age。
    """
    cf = _make_cf([_content_hash_route(upload_dir, versioned_max_age=3600)])
    with mock.patch.object(cf, "_list_objects", return_value={}):
        cf._upload_route(cf.context.routes[0])

    assert cf.s3_client.upload_file.call_count == 2
    for call in cf.s3_client.upload_file.call_args_list:
        extra = call.kwargs["ExtraArgs"]
        assert extra["CacheControl"] == "public, max-age=3600, immutable"
        assert extra["ContentType"] == "image/svg+xml"


def test_plain_route_upload_has_no_cache_control(upload_dir: Path):
    """versioning 無し・SPA でない route は従来どおり Cache-Control を付けない"""
    cf = _make_cf([_route(upload_dir)])
    with mock.patch.object(cf, "_list_objects", return_value={}):
        cf._upload_route(cf.context.routes[0])

    for call in cf.s3_client.upload_file.call_args_list:
        assert "CacheControl" not in call.kwargs["ExtraArgs"]


def test_stale_keys_lists_objects_missing_locally(upload_dir: Path):
    """stale_keys はローカルに無い key だけを sorted で返し、何もアップロードしない"""
    cf = _make_cf([_content_hash_route(upload_dir)])
    existing = {
        "v2/_app/immutable/a.svg": _md5(upload_dir / "a.svg"),
        "v2/_app/immutable/b.svg": "whatever",
        "v2/_app/immutable/z-old.js": "whatever",
        "v2/_app/immutable/chunks/old.js": "whatever",
    }
    with mock.patch.object(cf, "_list_objects", return_value=existing):
        stale = cf.stale_keys(cf.context.routes[0])

    assert stale == ["v2/_app/immutable/chunks/old.js", "v2/_app/immutable/z-old.js"]
    cf.s3_client.upload_file.assert_not_called()
    cf.s3_client.delete_object.assert_not_called()


def _run_prune(cf: CloudFront, args: list[str]):
    from click.testing import CliRunner
    from pocket_cli.cli import cloudfront_cli

    with mock.patch.object(
        cloudfront_cli, "get_cloudfront_resources", return_value=[cf]
    ):
        return CliRunner().invoke(
            cloudfront_cli.cloudfront, ["prune", "--stage", "dev", *args]
        )


def test_prune_deletes_stale_keys_with_yes(upload_dir: Path):
    """prune -y はローカルに無い key を削除する (delete_stale=false の掃除用)"""
    cf = _make_cf([_content_hash_route(upload_dir)])
    existing = {
        "v2/_app/immutable/a.svg": _md5(upload_dir / "a.svg"),
        "v2/_app/immutable/old.js": "whatever",
    }
    with mock.patch.object(cf, "_list_objects", return_value=existing):
        result = _run_prune(cf, ["-y"])

    assert result.exit_code == 0, result.output
    cf.s3_client.delete_object.assert_called_once_with(
        Bucket="dev-testprj-bucket", Key="v2/_app/immutable/old.js"
    )
    assert "1 件を削除しました" in result.output


def test_prune_dry_run_deletes_nothing(upload_dir: Path):
    """--dry-run は一覧を出すだけで削除しない"""
    cf = _make_cf([_content_hash_route(upload_dir)])
    existing = {"v2/_app/immutable/old.js": "whatever"}
    with mock.patch.object(cf, "_list_objects", return_value=existing):
        result = _run_prune(cf, ["--dry-run"])

    assert result.exit_code == 0, result.output
    cf.s3_client.delete_object.assert_not_called()
    assert "s3://dev-testprj-bucket/v2/_app/immutable/old.js" in result.output
    assert "--dry-run" in result.output


def test_prune_aborts_without_confirmation(upload_dir: Path):
    """確認プロンプトで No なら削除しない"""
    cf = _make_cf([_content_hash_route(upload_dir)])
    existing = {"v2/_app/immutable/old.js": "whatever"}
    from click.testing import CliRunner
    from pocket_cli.cli import cloudfront_cli

    with (
        mock.patch.object(cf, "_list_objects", return_value=existing),
        mock.patch.object(
            cloudfront_cli, "get_cloudfront_resources", return_value=[cf]
        ),
    ):
        result = CliRunner().invoke(
            cloudfront_cli.cloudfront, ["prune", "--stage", "dev"], input="n\n"
        )

    assert result.exit_code != 0
    cf.s3_client.delete_object.assert_not_called()


def test_prune_route_filter_and_unknown_route(upload_dir: Path):
    """--route は path_pattern で絞り、当たらなければエラーにする"""
    immutable = _content_hash_route(upload_dir)
    other = RouteContext(path_pattern="/twemoji/*", upload_dir=str(upload_dir))
    cf = _make_cf([immutable, other])
    existing = {
        "v2/_app/immutable/old.js": "whatever",
        "twemoji/old.svg": "whatever",
    }

    def list_objects(prefix: str) -> dict[str, str]:
        return {k: v for k, v in existing.items() if k.startswith(prefix + "/")}

    with mock.patch.object(cf, "_list_objects", side_effect=list_objects):
        result = _run_prune(cf, ["-y", "--route", "/v2/_app/immutable/*"])
    assert result.exit_code == 0, result.output
    cf.s3_client.delete_object.assert_called_once_with(
        Bucket="dev-testprj-bucket", Key="v2/_app/immutable/old.js"
    )

    with mock.patch.object(cf, "_list_objects", side_effect=list_objects):
        result = _run_prune(cf, ["-y", "--route", "/nothing/*"])
    assert result.exit_code != 0
    assert "アップロード対象" in result.output


def test_prune_reports_nothing_to_delete(upload_dir: Path):
    """旧版が無ければ削除せず、その旨を表示して終わる"""
    cf = _make_cf([_content_hash_route(upload_dir)])
    existing = {"v2/_app/immutable/a.svg": _md5(upload_dir / "a.svg")}
    with mock.patch.object(cf, "_list_objects", return_value=existing):
        result = _run_prune(cf, [])

    assert result.exit_code == 0, result.output
    cf.s3_client.delete_object.assert_not_called()
    assert "削除対象の旧版はありません" in result.output


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
