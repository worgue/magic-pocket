"""object_hash の key 名と URL encode の規則。

Rust (`magic_pocket_rs::static_url`) と同じ規則で、`tests/data/static_url_vectors.json`
を両方のテストが読む。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import quote

# key 名に埋める内容 hash (SHA-256 hex) の長さ
HASH_LEN = 16

_HASHED_BASENAME = re.compile(
    r"^(?P<stem>.+)\.(?P<hash>[0-9a-f]{16})(?P<ext>\.[^./]+)?$"
)


def hashed_name(path: str, sha256_hex: str) -> str:
    """論理 path に内容 hash を埋めた key 名を返す。

    `<dir>/<stem>.<hash16>.<ext>`。拡張子が無ければ `<stem>.<hash16>`。先頭の dot だけ
    の名前 (`.htaccess`) は拡張子ではなく名前として扱い `.htaccess.<hash16>` にする。
    `archive.tar.gz` は最後の dot で分けて `archive.tar.<hash16>.gz`。
    """
    if len(sha256_hex) < HASH_LEN:
        raise ValueError("sha256_hex が短すぎます: %r" % sha256_hex)
    digest = sha256_hex[:HASH_LEN]
    head, sep, base = path.rpartition("/")
    dot = base.rfind(".")
    if dot > 0:
        base = "%s.%s%s" % (base[:dot], digest, base[dot:])
    else:
        base = "%s.%s" % (base, digest)
    return head + sep + base


def is_hashed_name(path: str) -> bool:
    """`hashed_name` の形 (basename に `.<16hex>` を含む) かどうか。"""
    base = path.rpartition("/")[2]
    return _HASHED_BASENAME.match(base) is not None


def encode_url_path(path: str) -> str:
    """URL の path 部分に埋める形に percent-encode する。

    `A-Za-z0-9-._~/` 以外をすべて encode する (RFC 3986 の unreserved と `/`)。
    `urllib.parse.quote(path, safe="/")` と同じ。日本語は UTF-8 の byte 単位。
    """
    return quote(path, safe="/")


def validate_logical_path(path: str) -> None:
    """map / source の論理 path として使える形かを検査する。"""
    if not path:
        raise ValueError("path が空です")
    if path.startswith("/"):
        raise ValueError("path は先頭に / を付けません: %r" % path)
    if path.endswith("/"):
        raise ValueError("path は末尾に / を付けません: %r" % path)
    for segment in path.split("/"):
        if segment in ("", ".", ".."):
            raise ValueError("path に空・. ・.. の要素は使えません: %r" % path)
    if any(ch in path for ch in "\t\n\r\\"):
        raise ValueError("path にタブ・改行・バックスラッシュは使えません: %r" % path)


def sha256_file(file: Path, chunk_size: int = 1024 * 1024) -> str:
    """ファイル内容の SHA-256 hex。全量をメモリに持たずに読む。"""
    digest = hashlib.sha256()
    with file.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
