"""内容 hash の immutable key で静的ファイルを配信する (`versioning = "object_hash"`)。

- naming: key 名と URL encode の規則 (Rust 実装と test vector を共有)
- mapfile: path → 内容 hash の map (git に commit する release の正本)
- release: S3 に置く release 表と current ポインタの形
- resolver: runtime の `static_url(path)`
"""

from .resolver import StaticPathNotFound, StaticResolver, get_static_resolver

__all__ = ["StaticPathNotFound", "StaticResolver", "get_static_resolver"]
