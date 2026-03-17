import mimetypes
from pathlib import Path

from django.conf import settings
from django.http import HttpResponse
from django.urls import path

from ..utils import get_stage


def _resolve_managed_assets_dir(stage: str) -> Path | None:
    """managed_assets ディレクトリを解決する。

    {PROJECT_DIR}/managed_assets/{stage}/ があればそれを使い、
    なければ {PROJECT_DIR}/managed_assets/default/ にフォールバックする。
    どちらも存在しなければ None を返す。
    """
    base = settings.PROJECT_DIR / "managed_assets"
    if not base.is_dir():
        return None
    stage_dir = base / stage
    if stage_dir.is_dir():
        return stage_dir
    default_dir = base / "default"
    if default_dir.is_dir():
        return default_dir
    return None


def _as_path(asset_dir: Path, filename: str):
    """ファイルを返す Django URL パターンを生成する"""
    file_path = asset_dir / filename
    filetype = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    def render(request):
        return HttpResponse(file_path.read_bytes(), content_type=filetype)

    return path(filename, render)


def get_managed_assets_urls() -> list:
    """managed_assets ディレクトリからURLパターンを生成する"""
    stage = get_stage()
    asset_dir = _resolve_managed_assets_dir(stage)
    if asset_dir is None:
        return []
    return [_as_path(asset_dir, f.name) for f in asset_dir.iterdir() if f.is_file()]


# 後方互換エイリアス
get_pocket_http_urls = get_managed_assets_urls
