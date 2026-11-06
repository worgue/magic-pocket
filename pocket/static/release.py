"""S3 に置く release 表と current ポインタ。

route ごとの metadata prefix (`RouteContext.static_meta_prefix`) の下に
- `releases/<release id>.json.gz`:
  `{"version": 1, "release": id, "entries": {path: rel_key}}`
- `current`: `{"version": 1, "release": id, "map_sha256": ..., "published_at": ...}`
を置く。rel_key は route の key prefix からの相対 key で、origin_path に依存しない。
runtime は current → release の順に読み、current の ETag で更新を検知する。
"""

from __future__ import annotations

import gzip
import json
from typing import Any

from .mapfile import StaticMap

RELEASE_VERSION = 1


def release_key(meta_prefix: str, release_id: str) -> str:
    return "%sreleases/%s.json.gz" % (meta_prefix, release_id)


def current_key(meta_prefix: str) -> str:
    return meta_prefix + "current"


def build_release_document(static_map: StaticMap) -> dict[str, Any]:
    return {
        "version": RELEASE_VERSION,
        "release": static_map.release_id,
        "route": static_map.route,
        "entries": {e.path: e.rel_key for e in static_map.sorted_entries()},
    }


def encode_release(document: dict[str, Any]) -> bytes:
    text = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return gzip.compress(text.encode("utf-8"), mtime=0)


def decode_release(data: bytes) -> dict[str, Any]:
    document = json.loads(gzip.decompress(data).decode("utf-8"))
    _check_version(document, "release")
    if not isinstance(document.get("entries"), dict):
        raise ValueError("release 表に entries がありません")
    return document


def build_current_document(static_map: StaticMap, published_at: str) -> dict[str, Any]:
    return {
        "version": RELEASE_VERSION,
        "release": static_map.release_id,
        "map_sha256": static_map.content_sha256,
        "published_at": published_at,
    }


def encode_current(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("utf-8")


def decode_current(data: bytes) -> dict[str, Any]:
    document = json.loads(data.decode("utf-8"))
    _check_version(document, "current")
    if not isinstance(document.get("release"), str):
        raise ValueError("current に release がありません")
    return document


def _check_version(document: Any, what: str) -> None:
    if not isinstance(document, dict):
        raise ValueError("%s が JSON object ではありません" % what)
    version = document.get("version")
    if version != RELEASE_VERSION:
        raise ValueError(
            "%s の version %r は読めません (対応: %d)。pocket の版を揃えてください"
            % (what, version, RELEASE_VERSION)
        )
