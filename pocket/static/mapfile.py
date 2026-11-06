"""path → 内容 hash の map ファイル。

git に commit する release の正本。テキストで path 順に並べ 1 行 1 entry
(`path<TAB>sha256<TAB>source<TAB>H|P`) なので、変更が少ないときの diff が小さい。
release id は正規化した本文の SHA-256 の先頭 16 hex で、同じ内容なら同じ id になる。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .naming import HASH_LEN, hashed_name, validate_logical_path

MAP_FORMAT_LINE = "# magic-pocket static map v1"
_SHA256_LEN = 64


@dataclass(frozen=True)
class MapEntry:
    path: str
    sha256: str
    source: str
    hashed: bool

    @property
    def rel_key(self) -> str:
        """route の key prefix からの相対 key (= URL の prefix からの相対 path)。"""
        if self.hashed:
            return hashed_name(self.path, self.sha256)
        return self.path

    def line(self) -> str:
        return "%s\t%s\t%s\t%s" % (
            self.path,
            self.sha256,
            self.source,
            "H" if self.hashed else "P",
        )


class StaticMapError(ValueError):
    pass


class StaticMap:
    def __init__(self, route: str, entries: dict[str, MapEntry] | None = None):
        # route は "<cloudfront 名>/<route 名>"。別 route の map の取り違えに気付くため
        self.route = route
        self.entries: dict[str, MapEntry] = dict(entries or {})

    def add(self, entry: MapEntry) -> None:
        validate_logical_path(entry.path)
        if len(entry.sha256) != _SHA256_LEN or any(
            ch not in "0123456789abcdef" for ch in entry.sha256
        ):
            raise StaticMapError("sha256 の形が不正です: %r" % entry.sha256)
        if entry.path in self.entries:
            raise StaticMapError("path が重複しています: %r" % entry.path)
        self.entries[entry.path] = entry

    def sorted_entries(self) -> list[MapEntry]:
        return [self.entries[p] for p in sorted(self.entries)]

    def body(self) -> str:
        return "".join(e.line() + "\n" for e in self.sorted_entries())

    def dumps(self) -> str:
        return "%s\n# route: %s\n%s" % (MAP_FORMAT_LINE, self.route, self.body())

    @property
    def release_id(self) -> str:
        """release の識別子。map の本文 (route 名を含む) から決まる。"""
        return self.content_sha256[:HASH_LEN]

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.dumps().encode("utf-8")).hexdigest()

    @classmethod
    def loads(cls, text: str) -> StaticMap:
        lines = text.splitlines()
        if not lines or lines[0] != MAP_FORMAT_LINE:
            raise StaticMapError(
                "map の形式が違います (先頭行が %r ではありません)。pocket の版が"
                " 古いか、map ファイルではありません" % MAP_FORMAT_LINE
            )
        route = ""
        static_map = cls(route)
        for lineno, line in enumerate(lines[1:], start=2):
            if not line.strip():
                continue
            if line.startswith("#"):
                if line.startswith("# route: "):
                    static_map.route = line[len("# route: ") :].strip()
                continue
            parts = line.split("\t")
            if len(parts) != 4 or parts[3] not in ("H", "P"):
                raise StaticMapError("map の %d 行目を読めません: %r" % (lineno, line))
            try:
                static_map.add(
                    MapEntry(
                        path=parts[0],
                        sha256=parts[1],
                        source=parts[2],
                        hashed=parts[3] == "H",
                    )
                )
            except ValueError as e:
                raise StaticMapError("map の %d 行目: %s" % (lineno, e)) from e
        if not static_map.route:
            raise StaticMapError("map に `# route:` 行がありません")
        return static_map
