"""CloudFront.upload の build subprocess 失敗時の UX 検証。

`pocket deploy` で `subprocess.run(route.build, ..., check=True)` が落ちた
ときに、生 traceback だけで放り投げるのではなく actionable な hint を
echo に出してから再 raise することを確認する。
"""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest
from pocket_cli.resources.cloudfront import CloudFront

from pocket.context import CloudFrontContext, RouteContext


def _make_cf() -> CloudFront:
    ctx = CloudFrontContext(
        name="web",
        region="ap-northeast-1",
        s3_region="ap-northeast-1",
        stage="dev",
        slug="dev-testprj-web",
        bucket_name="dev-testprj-bucket",
        resource_prefix="dev-testprj-",
        routes=[
            RouteContext(
                is_default=True,
                is_spa=True,
                origin_path="/app",
                build="just frontend-build",
                build_dir="frontend/dist",
            ),
        ],
    )
    with mock.patch("boto3.client"):
        return CloudFront(ctx)


def test_upload_build_failure_emits_actionable_hint_and_reraises(capsys):
    """build コマンドが exit!=0 で終わった場合、(1) 失敗メッセージ + (2)
    依存再インストール hint を echo してから (3) 元の CalledProcessError を
    re-raise する。"""
    cf = _make_cf()
    err = subprocess.CalledProcessError(returncode=1, cmd="just frontend-build")

    with mock.patch("subprocess.run", side_effect=err):
        with pytest.raises(subprocess.CalledProcessError):
            cf.upload()

    out = capsys.readouterr().out
    # (1) 失敗メッセージに exit code とコマンド文字列が含まれる
    assert "build コマンドが失敗" in out
    assert "exit 1" in out
    assert "just frontend-build" in out
    # (2) 依存再インストール hint (rolldown 等の optional dep を例示)
    assert "依存を入れ直して" in out
    assert "node_modules" in out


def test_upload_skip_build_does_not_invoke_subprocess():
    """--skip-build 相当 (skip_build=True) では subprocess は呼ばれない
    = build error hint も流れない。回帰確認。"""
    cf = _make_cf()
    with (
        mock.patch("subprocess.run") as run,
        mock.patch.object(cf, "_upload_route"),
        mock.patch.object(cf, "_invalidate"),
    ):
        cf.upload(skip_build=True)
        run.assert_not_called()
