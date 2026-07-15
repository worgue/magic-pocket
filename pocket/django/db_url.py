"""DATABASE_URL の解析ヘルパ (runtime 内で共有)。

生成側 (`pocket.runtime._set_rds_database_url` / `pocket.provisioning.neon`) は
user / password を percent-encode して URL に埋め込むため、解析側は必ず
unquote する。生成と解析を非対称にしないための単一実装。
"""

from __future__ import annotations

import urllib.parse


def parse_database_url_credentials(database_url: str) -> dict[str, str]:
    """DATABASE_URL から Django settings_dict 形式の接続情報を取り出す。"""
    parsed = urllib.parse.urlparse(database_url)
    return {
        "USER": urllib.parse.unquote(parsed.username or ""),
        "PASSWORD": urllib.parse.unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or ""),
        "NAME": parsed.path.lstrip("/"),
    }
