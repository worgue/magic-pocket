"""`versioning = "object_hash"` の route の publish エンジン。

ローカルの sources を hash し、内容 hash 付きの key で S3 に上げ (上書き禁止)、
map (git に commit する release の正本) と S3 の release 表 / current を揃える。

- 同じ key に別の内容は載らない: PutObject は `If-None-Match: *` と `ChecksumSHA256`
  を付け、412 なら既存の checksum を map の hash と照合する
- 変更なしの再 publish は upload 0 件: S3 の一覧に無い key だけ上げる
- 途中で止まっても再実行で続きから進む: 上げ終わった key は次回の一覧で除かれる
- hash 後にローカルが変わっても壊れない: upload は checksum で S3 が拒否し、adopt は
  CopySourceIfMatch で元を固定する。失敗したら current を更新しない
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

import boto3
import pathspec
from botocore.exceptions import ClientError

from pocket.static.mapfile import MapEntry, StaticMap, StaticMapError
from pocket.static.naming import validate_logical_path
from pocket.static.release import (
    build_current_document,
    build_release_document,
    current_key,
    decode_current,
    decode_release,
    encode_current,
    encode_release,
    release_key,
)
from pocket.utils import echo

if TYPE_CHECKING:
    from pocket.context import RouteContext
    from pocket.settings import StaticSource

# 単一 PUT の上限。第 1 段は multipart を使わない
_SINGLE_PUT_LIMIT = 5 * 1024 * 1024 * 1024
_CHUNK = 1024 * 1024
_DELETE_BATCH = 1000


class StaticPublishError(Exception):
    pass


@dataclass
class LocalFile:
    path: str  # 論理 path (= map の path)
    file: Path
    source: StaticSource
    size: int
    sha256: str = ""
    md5: str = ""

    @property
    def entry(self) -> MapEntry:
        return MapEntry(
            path=self.path,
            sha256=self.sha256,
            source=self.source.name,
            hashed=self.source.hashed,
        )


@dataclass
class PublishResult:
    release_id: str
    hashed: int = 0
    uploaded: int = 0
    copied: int = 0
    skipped: int = 0
    verified: int = 0
    errors: list[str] = field(default_factory=list)


def _digest_file(file: Path) -> tuple[str, str]:
    sha = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    with file.open("rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            sha.update(chunk)
            md5.update(chunk)
    return sha.hexdigest(), md5.hexdigest()


def _b64_sha256(sha256_hex: str) -> str:
    return base64.b64encode(bytes.fromhex(sha256_hex)).decode("ascii")


def _is_plain_md5_etag(etag: str | None) -> bool:
    return bool(etag) and "-" not in (etag or "")


def _strip_quotes(etag: str) -> str:
    return etag.strip('"')


class StaticPublisher:
    def __init__(
        self,
        route: RouteContext,
        *,
        bucket: str,
        region: str,
        base_dir: Path,
        jobs: int = 8,
        s3_client: Any = None,
    ) -> None:
        if not route.is_object_hash:
            raise StaticPublishError(
                'route %s は versioning = "object_hash" ではありません'
                % (route.path_pattern or "/*")
            )
        self.route = route
        self.bucket = bucket
        self.base_dir = base_dir
        self.jobs = max(1, jobs)
        self.s3 = s3_client or boto3.client("s3", region_name=region)

    # --- 設定から導く名前 ---

    @property
    def route_label(self) -> str:
        return "%s/%s" % (self.route.cloudfront_name, self.route.name)

    @property
    def map_file(self) -> Path:
        return self.base_dir / self.route.map_path

    @property
    def key_prefix(self) -> str:
        return self.route.key_prefix

    @property
    def meta_prefix(self) -> str:
        return self.route.static_meta_prefix

    @property
    def cache_control(self) -> str:
        return "public, max-age=%d, immutable" % self.route.versioned_max_age

    # --- ローカルの列挙と hash ---

    def enumerate_sources(self) -> list[LocalFile]:
        files: list[LocalFile] = []
        seen: dict[str, str] = {}
        for source in self.route.sources:
            src_dir = self.base_dir / source.dir
            if not src_dir.is_dir():
                raise StaticPublishError(
                    "source '%s' の dir が見つかりません: %s" % (source.name, src_dir)
                )
            include = (
                pathspec.PathSpec.from_lines("gitignore", source.include)
                if source.include
                else None
            )
            exclude = (
                pathspec.PathSpec.from_lines("gitignore", source.exclude)
                if source.exclude
                else None
            )
            for rel in _walk_files(src_dir):
                if include is not None and not include.match_file(rel):
                    continue
                if exclude is not None and exclude.match_file(rel):
                    continue
                logical = source.prefix + rel
                try:
                    validate_logical_path(logical)
                except ValueError as e:
                    raise StaticPublishError(
                        "source '%s' のファイル名を使えません: %s" % (source.name, e)
                    ) from e
                if logical in seen:
                    raise StaticPublishError(
                        "path %r が source '%s' と '%s' の両方にあります"
                        % (logical, seen[logical], source.name)
                    )
                seen[logical] = source.name
                file = src_dir / rel
                files.append(
                    LocalFile(
                        path=logical, file=file, source=source, size=file.stat().st_size
                    )
                )
        files.sort(key=lambda f: f.path)
        return files

    def hash_files(self, files: list[LocalFile]) -> None:
        for f in files:
            if f.size > _SINGLE_PUT_LIMIT:
                raise StaticPublishError(
                    "%s は 5GB を超えています (第 1 段は単一 PUT のみ)" % f.file
                )

        def work(f: LocalFile) -> None:
            f.sha256, f.md5 = _digest_file(f.file)

        with ThreadPoolExecutor(max_workers=self.jobs) as pool:
            list(pool.map(work, files))

    def build_map(self, files: list[LocalFile]) -> StaticMap:
        static_map = StaticMap(self.route_label)
        for f in files:
            static_map.add(f.entry)
        _check_rel_key_collisions(static_map)
        return static_map

    # --- map ファイル ---

    def write_map(self, static_map: StaticMap) -> Path:
        self.map_file.parent.mkdir(parents=True, exist_ok=True)
        self.map_file.write_text(static_map.dumps(), encoding="utf-8")
        return self.map_file

    def read_map(self) -> StaticMap:
        if not self.map_file.is_file():
            raise StaticPublishError(
                "map ファイルがありません: %s\n"
                "  初回は `pocket static publish --hash-generate` か"
                " `pocket static hash generate` で作ってください" % self.map_file
            )
        try:
            static_map = StaticMap.loads(self.map_file.read_text(encoding="utf-8"))
        except StaticMapError as e:
            raise StaticPublishError("%s: %s" % (self.map_file, e)) from e
        if static_map.route != self.route_label:
            raise StaticPublishError(
                "%s は route %s の map です (この route は %s)"
                % (self.map_file, static_map.route, self.route_label)
            )
        _check_rel_key_collisions(static_map)
        return static_map

    def map_dirty_reason(self) -> str | None:
        """map が git で未 commit / 作業ツリーと差があれば理由を返す。git 外は None。"""
        try:
            result = subprocess.run(  # noqa: S603 shell=False + 制御された引数
                ["git", "status", "--porcelain", "--", str(self.map_file)],  # noqa: S607 git は PATH 前提の標準ツール
                cwd=self.base_dir,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            echo.warning("git が無いため map の commit 状態を確認できません")
            return None
        if result.returncode != 0:
            echo.warning(
                "git repo ではないため map の commit 状態を確認できません: %s"
                % result.stderr.strip()
            )
            return None
        status = result.stdout.strip()
        if not status:
            return None
        if status.startswith("??"):
            return "map が git に追加されていません (untracked)"
        return "map が commit 済みの内容と違います (%s)" % status.split()[0]

    # --- S3 ---

    def list_existing(self) -> dict[str, str]:
        """route の key prefix 配下の {key: ETag}。"""
        existing: dict[str, str] = {}
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.key_prefix):
            for obj in page.get("Contents", []):
                existing[obj["Key"]] = obj.get("ETag", "")
        return existing

    def _head_checksum(self, key: str) -> tuple[str | None, str | None]:
        """既存 object の (ChecksumSHA256 base64 (FULL_OBJECT のみ), ETag)。"""
        head = self.s3.head_object(Bucket=self.bucket, Key=key, ChecksumMode="ENABLED")
        checksum = head.get("ChecksumSHA256")
        checksum_type = head.get("ChecksumType")
        if checksum and checksum_type not in (None, "FULL_OBJECT"):
            checksum = None
        return checksum, head.get("ETag")

    def verify_existing(self, key: str, entry: MapEntry, md5: str | None) -> None:
        """S3 にある key の内容が map の hash と一致することを確かめる。"""
        checksum, etag = self._head_checksum(key)
        if checksum is not None:
            if checksum != _b64_sha256(entry.sha256):
                raise StaticPublishError(
                    "s3://%s/%s の内容が map の hash と違います (path %r)。"
                    "内容を変えたなら名前も変えてください"
                    % (self.bucket, key, entry.path)
                )
            return
        if md5 is not None and _is_plain_md5_etag(etag):
            if _strip_quotes(etag or "") != md5:
                raise StaticPublishError(
                    "s3://%s/%s の内容がローカルと違います (path %r)。"
                    "内容を変えたなら名前も変えてください"
                    % (self.bucket, key, entry.path)
                )
            return
        raise StaticPublishError(
            "s3://%s/%s の内容を確かめられません (SHA-256 checksum が無く、ETag も"
            " MD5 ではありません)。path %r" % (self.bucket, key, entry.path)
        )

    def _put(self, key: str, local: LocalFile) -> None:
        content_type = mimetypes.guess_type(local.path)[0] or "application/octet-stream"
        with local.file.open("rb") as body:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                IfNoneMatch="*",
                ChecksumSHA256=_b64_sha256(local.sha256),
                ContentType=content_type,
                CacheControl=self.cache_control,
            )

    def _upload_one(self, key: str, local: LocalFile) -> str:
        """1 件 upload する。戻り値は "uploaded" | "verified"。"""
        try:
            self._put(key, local)
            return "uploaded"
        except ClientError as e:
            if e.response["Error"]["Code"] != "PreconditionFailed":
                raise
        # 並行する publish が先に置いた (内容 hash が同じなら同じ内容のはず)。照合する
        self.verify_existing(key, local.entry, local.md5)
        return "verified"

    def _copy_from_flat(self, key: str, local: LocalFile, flat_etag: str) -> str | None:
        """平置き key から server-side copy する。できなければ None。"""
        if not _is_plain_md5_etag(flat_etag) or _strip_quotes(flat_etag) != local.md5:
            return None
        flat_key = self.key_prefix + local.path
        content_type = mimetypes.guess_type(local.path)[0] or "application/octet-stream"
        try:
            self.s3.copy_object(
                Bucket=self.bucket,
                Key=key,
                CopySource={"Bucket": self.bucket, "Key": flat_key},
                CopySourceIfMatch=flat_etag,
                MetadataDirective="REPLACE",
                ContentType=content_type,
                CacheControl=self.cache_control,
                ChecksumAlgorithm="SHA256",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "PreconditionFailed":
                return None
            raise
        self.verify_existing(key, local.entry, local.md5)
        return "copied"

    # --- publish ---

    def publish(
        self,
        *,
        hash_generate: bool = False,
        allow_dirty: bool = False,
        adopt: bool = False,
        verify_existing: bool = False,
    ) -> PublishResult:
        # 開始時点の current。最後の If-Match に同じ値を使い、途中で別の publish が
        # 切り替えていたら競合として止める
        current_etag = self._current_etag()

        files = self.enumerate_sources()
        echo.info("[%s] %d ファイルを hash します" % (self.route_label, len(files)))
        self.hash_files(files)
        local_map = self.build_map(files)
        by_path = {f.path: f for f in files}

        if hash_generate:
            static_map = local_map
            self.write_map(static_map)
            echo.info(
                "map を書きました: %s (release %s)"
                % (self.map_file, static_map.release_id)
            )
        else:
            static_map = self._read_committed_map(allow_dirty)

        existing = self.list_existing()
        result = PublishResult(release_id=static_map.release_id, hashed=len(files))
        missing_local = self._check_against_map(static_map, by_path, existing)

        # P (名前のまま) は key に hash が無いので、既存 key の内容を必ず照合する
        for entry in missing_local:
            if not entry.hashed or verify_existing:
                self.verify_existing(self.key_prefix + entry.rel_key, entry, None)
                result.verified += 1
            else:
                result.skipped += 1

        targets: list[tuple[str, LocalFile]] = []
        for path in sorted(by_path):
            local = by_path[path]
            key = self.key_prefix + local.entry.rel_key
            if key in existing:
                if not local.source.hashed or verify_existing:
                    self.verify_existing(key, local.entry, local.md5)
                    result.verified += 1
                else:
                    result.skipped += 1
                continue
            targets.append((key, local))

        echo.info(
            "[%s] upload %d 件 (skip %d, 照合 %d)"
            % (self.route_label, len(targets), result.skipped, result.verified)
        )
        self._transfer(targets, existing, adopt, result)
        self._write_release(static_map, current_etag)
        echo.success(
            "[%s] release %s を公開しました (upload %d, copy %d, skip %d, 照合 %d)"
            % (
                self.route_label,
                static_map.release_id,
                result.uploaded,
                result.copied,
                result.skipped,
                result.verified,
            )
        )
        return result

    def _read_committed_map(self, allow_dirty: bool) -> StaticMap:
        static_map = self.read_map()
        reason = self.map_dirty_reason()
        if reason and not allow_dirty:
            raise StaticPublishError(
                "%s: %s。commit してから publish するか、--allow-dirty を付けて"
                "ください" % (self.map_file, reason)
            )
        if reason:
            echo.warning("%s: %s (--allow-dirty)" % (self.map_file, reason))
        return static_map

    def _check_against_map(
        self,
        static_map: StaticMap,
        by_path: dict[str, LocalFile],
        existing: dict[str, str],
    ) -> list[MapEntry]:
        """ローカルと map を照合する。戻り値は map にだけある (S3 に key がある) entry。

        ローカルにあるものは map と一致していること。map にだけあるものは S3 に
        key があればよい (2 回目以降の publish は全ファイルが無くても通る)。
        """
        errors: list[str] = []
        missing_local: list[MapEntry] = []
        for path, local in by_path.items():
            entry = static_map.entries.get(path)
            if entry is None:
                errors.append("map に無いファイル: %s" % path)
            elif entry != local.entry:
                errors.append(
                    "map と違う: %s (map: %s / local: %s)"
                    % (path, _describe(entry), _describe(local.entry))
                )
        for path, entry in static_map.entries.items():
            if path in by_path:
                continue
            key = self.key_prefix + entry.rel_key
            if key not in existing:
                errors.append(
                    "map にあるがローカルにも S3 にも無い: %s (%s)" % (path, key)
                )
            else:
                missing_local.append(entry)
        if errors:
            raise StaticPublishError(
                "ローカルと map が一致しません (%d 件):\n  %s"
                % (len(errors), "\n  ".join(errors[:50]))
            )
        return missing_local

    def _transfer(
        self,
        targets: list[tuple[str, LocalFile]],
        existing: dict[str, str],
        adopt: bool,
        result: PublishResult,
    ) -> None:
        def work(item: tuple[str, LocalFile]) -> str:
            key, local = item
            if adopt:
                flat_key = self.key_prefix + local.path
                if flat_key in existing:
                    outcome = self._copy_from_flat(key, local, existing[flat_key])
                    if outcome:
                        return outcome
            return self._upload_one(key, local)

        with ThreadPoolExecutor(max_workers=self.jobs) as pool:
            for i, outcome in enumerate(pool.map(work, targets), start=1):
                if outcome == "uploaded":
                    result.uploaded += 1
                elif outcome == "copied":
                    result.copied += 1
                else:
                    result.verified += 1
                if i % 500 == 0 or i == len(targets):
                    echo.log("  %d / %d" % (i, len(targets)))

    def _current_etag(self) -> str | None:
        try:
            head = self.s3.head_object(
                Bucket=self.bucket, Key=current_key(self.meta_prefix)
            )
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return head["ETag"]

    def _write_release(self, static_map: StaticMap, current_etag: str | None) -> None:
        rel_key = release_key(self.meta_prefix, static_map.release_id)
        try:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=rel_key,
                Body=encode_release(build_release_document(static_map)),
                IfNoneMatch="*",
                ContentType="application/json",
                ContentEncoding="gzip",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "PreconditionFailed":
                raise
        published_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        body = encode_current(build_current_document(static_map, published_at))
        kwargs: dict[str, Any] = {}
        if current_etag is None:
            kwargs["IfNoneMatch"] = "*"
        else:
            kwargs["IfMatch"] = current_etag
        try:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=current_key(self.meta_prefix),
                Body=body,
                ContentType="application/json",
                **kwargs,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "PreconditionFailed":
                raise StaticPublishError(
                    "別の publish が同じ route の current を更新しました。"
                    "内容を確かめてから再実行してください"
                ) from e
            raise

    # --- status / purge ---

    def current(self) -> dict[str, Any] | None:
        try:
            res = self.s3.get_object(
                Bucket=self.bucket, Key=current_key(self.meta_prefix)
            )
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise
        return decode_current(res["Body"].read())

    def recorded_release_keys(self) -> set[str]:
        """metadata に記録された全 release が参照する key (purge の対象)。"""
        keys: set[str] = set()
        paginator = self.s3.get_paginator("list_objects_v2")
        prefix = self.meta_prefix + "releases/"
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                res = self.s3.get_object(Bucket=self.bucket, Key=obj["Key"])
                document = decode_release(res["Body"].read())
                for rel in document["entries"].values():
                    keys.add(self.key_prefix + rel)
        return keys

    def purge(self, *, dry_run: bool = False) -> list[str]:
        keys = sorted(self.recorded_release_keys() & set(self.list_existing()))
        if dry_run or not keys:
            return keys
        failed: list[str] = []
        for i in range(0, len(keys), _DELETE_BATCH):
            batch = keys[i : i + _DELETE_BATCH]
            res = self.s3.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
            )
            # Quiet でも失敗した key は Errors に入る。HTTP 200 なので自分で見る
            failed.extend(
                "%s: %s %s"
                % (err.get("Key", "?"), err.get("Code", "?"), err.get("Message", ""))
                for err in res.get("Errors", [])
            )
        if failed:
            raise StaticPublishError(purge_failure_message(failed))
        return keys


def purge_failure_message(failed: list[str]) -> str:
    """DeleteObjects が一部の key を消せなかったときのエラー文 (Rust 版と同文)。"""
    return "%d 件の削除に失敗しました (purge を再実行すると残りを対象にします):\n%s" % (
        len(failed),
        "\n".join("  " + f for f in failed),
    )


def _describe(entry: MapEntry) -> str:
    return "%s %s %s" % (entry.sha256[:16], entry.source, "H" if entry.hashed else "P")


def _walk_files(root: Path) -> Iterable[str]:
    """root 配下のファイルを相対 posix path で返す。symlink は追わず .pocket は除く。"""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d != ".pocket" and not (Path(dirpath) / d).is_symlink()
        )
        for name in sorted(filenames):
            file = Path(dirpath) / name
            if file.is_symlink() or not file.is_file():
                continue
            yield file.relative_to(root).as_posix()


def _check_rel_key_collisions(static_map: StaticMap) -> None:
    seen: dict[str, str] = {}
    for entry in static_map.sorted_entries():
        rel = entry.rel_key
        if rel in seen:
            raise StaticPublishError(
                "key %r が path %r と %r の両方から導かれます"
                % (rel, seen[rel], entry.path)
            )
        seen[rel] = entry.path


def status_report(publisher: StaticPublisher) -> dict[str, Any]:
    """`pocket static status` の表示用。"""
    report: dict[str, Any] = {
        "route": publisher.route_label,
        "bucket": publisher.bucket,
        "key_prefix": publisher.key_prefix,
        "map": str(publisher.map_file),
    }
    if publisher.map_file.is_file():
        try:
            local = publisher.read_map()
            report["map_release"] = local.release_id
            report["map_entries"] = len(local.entries)
        except StaticPublishError as e:
            report["map_error"] = str(e)
    current = publisher.current()
    if current:
        report["current_release"] = current["release"]
        report["published_at"] = current.get("published_at")
    report["in_sync"] = bool(
        current and report.get("map_release") == current["release"]
    )
    return json.loads(json.dumps(report))
