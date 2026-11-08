"""runtime の `static_url(path)`。

S3 の current → release 表を読み、論理 path を内容 hash 付きの URL にする。表に無い
path は `StaticPathNotFound` (hash なしの URL を黙って返さない)。初回の取得失敗は
例外で、次の呼び出しで再試行する。以降は呼び出し時に経過時間 (既定 60 秒) を見て
current を HEAD し、ETag が変わったときだけ release 表を 1 本読み直して差し替える。
読み直しの失敗では前の版を使い続ける。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .naming import encode_url_path
from .release import current_key, decode_current, decode_release, release_key

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_INTERVAL = 60.0


class StaticPathNotFound(KeyError):
    """map に無い path を解決しようとした。"""

    def __init__(self, path: str, release_id: str):
        super().__init__(path)
        self.path = path
        self.release_id = release_id

    def __str__(self) -> str:
        return "静的ファイル %r は release %s の map にありません" % (
            self.path,
            self.release_id,
        )


class _Table:
    def __init__(self, release_id: str, etag: str, entries: dict[str, str]):
        self.release_id = release_id
        self.etag = etag
        self.entries = entries


class StaticResolver:
    def __init__(
        self,
        *,
        bucket: str,
        meta_prefix: str,
        base_url: str,
        refresh_interval: float = DEFAULT_REFRESH_INTERVAL,
        s3_client: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.bucket = bucket
        self.meta_prefix = meta_prefix
        # base_url は "https://<domain>/<url prefix>/" の形 (末尾 / 付き)
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.refresh_interval = refresh_interval
        self._s3 = s3_client
        self._clock = clock
        self._table: _Table | None = None
        self._checked_at = 0.0
        self._lock = threading.Lock()

    @property
    def s3(self) -> Any:
        if self._s3 is None:
            self._s3 = boto3.client("s3")
        return self._s3

    @property
    def release_id(self) -> str:
        return self._ensure_table().release_id

    def url(self, path: str) -> str:
        table = self._ensure_table()
        rel_key = table.entries.get(path)
        if rel_key is None:
            raise StaticPathNotFound(path, table.release_id)
        return self.base_url + encode_url_path(rel_key)

    def _ensure_table(self) -> _Table:
        table = self._table
        if table is None:
            with self._lock:
                if self._table is None:
                    self._table = self._load(etag=None)
                    self._checked_at = self._clock()
                return self._table
        if self._clock() - self._checked_at >= self.refresh_interval:
            self._refresh()
        return self._table or table

    def _refresh(self) -> None:
        # 同時に来た呼び出しは 1 つだけが S3 を見る。残りは今の表を使う
        if not self._lock.acquire(blocking=False):
            return
        try:
            self._checked_at = self._clock()
            current = self._table
            if current is None:
                return
            try:
                head = self.s3.head_object(
                    Bucket=self.bucket, Key=current_key(self.meta_prefix)
                )
                if head["ETag"] == current.etag:
                    return
                self._table = self._load(etag=head["ETag"])
            except (ClientError, BotoCoreError, ValueError) as e:
                logger.warning(
                    "static の release 表を読み直せません (前の版 %s を使います): %s",
                    current.release_id,
                    e,
                )
        finally:
            self._lock.release()

    def _load(self, *, etag: str | None) -> _Table:
        res = self.s3.get_object(Bucket=self.bucket, Key=current_key(self.meta_prefix))
        current = decode_current(res["Body"].read())
        release_id = current["release"]
        rel = self.s3.get_object(
            Bucket=self.bucket, Key=release_key(self.meta_prefix, release_id)
        )
        document = decode_release(rel["Body"].read())
        if document.get("release") != release_id:
            raise ValueError(
                "release 表 %s の中身の id が違います: %r"
                % (release_id, document.get("release"))
            )
        return _Table(release_id, res["ETag"], document["entries"])


_resolvers: dict[tuple[str, str | None], StaticResolver] = {}
_resolvers_lock = threading.Lock()


def get_static_resolver(
    distribution: str,
    route: str | None = None,
    *,
    stage: str | None = None,
    refresh_interval: float = DEFAULT_REFRESH_INTERVAL,
) -> StaticResolver:
    """pocket.toml の `[cloudfront.<distribution>]` の object_hash route の resolver。

    route は route の `ref`。省略時は distribution 内の object_hash route が 1 つ
    だけならそれ。配信ドメインは `[cloudfront.<name>].domain` (alias) →
    `POCKET_CLOUDFRONT_<NAME>_DOMAIN` (runtime が注入する distribution の既定ドメイン)
    の順に取る (django の `_resolve_cloudfront_domain` と同じ優先順。画面と同じ
    origin の URL を返すため)。同じ引数には同じ instance を返す (release 表の cache
    を共有する)。
    """
    cache_key = (distribution, route)
    with _resolvers_lock:
        resolver = _resolvers.get(cache_key)
        if resolver is None:
            resolver = _build_resolver(distribution, route, stage, refresh_interval)
            _resolvers[cache_key] = resolver
        return resolver


def _build_resolver(
    distribution: str, route: str | None, stage: str | None, refresh_interval: float
) -> StaticResolver:
    from ..runtime import get_context

    stage = stage or os.environ.get("POCKET_STAGE")
    if not stage:
        raise RuntimeError("stage が分かりません (POCKET_STAGE を設定してください)")
    context = get_context(stage=stage)
    cf = context.cloudfront.get(distribution)
    if cf is None:
        raise RuntimeError("cloudfront '%s' は設定にありません" % distribution)
    candidates = [r for r in cf.routes if r.is_object_hash]
    if route is not None:
        candidates = [r for r in candidates if r.ref == route]
    if len(candidates) != 1:
        raise RuntimeError(
            "cloudfront '%s' の object_hash route を特定できません (候補 %d 件)。"
            " route の ref を指定してください" % (distribution, len(candidates))
        )
    target = candidates[0]
    domain = cf.domain or os.environ.get(
        "POCKET_CLOUDFRONT_%s_DOMAIN" % distribution.upper()
    )
    if not domain:
        raise RuntimeError(
            "cloudfront '%s' の配信ドメインが分かりません"
            " (domain の宣言が無く POCKET_CLOUDFRONT_%s_DOMAIN も未設定)"
            % (distribution, distribution.upper())
        )
    return StaticResolver(
        bucket=cf.bucket_name,
        meta_prefix=target.static_meta_prefix,
        base_url="https://%s%s" % (domain, target.url_prefix),
        refresh_interval=refresh_interval,
    )
