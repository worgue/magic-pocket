from __future__ import annotations

import ipaddress


def parse_viewer_ip(value: str) -> str | None:
    """CloudFront 由来の viewer IP header から IP 部分を取り出して検証する。

    magic-pocket の CloudFront Function は `event.viewer.ip` (port 無しの素の IP)
    を `x-pocket-viewer-ip` に載せるため、通常は port 分解は不要。ただし
    `CloudFront-Viewer-Address` (`IP:port`) を直接転送する構成にも備えて
    "最後のコロンの後ろ = port" 規則で頑健にパースする。妥当な IP として検証
    できなければ None を返す。

    - IPv4:              '198.51.100.10'        -> '198.51.100.10'
    - IPv4 (port 付き):   '198.51.100.10:443'    -> '198.51.100.10'
    - IPv6 (素):          '2001:db8::1'          -> '2001:db8::1'
    - IPv6 (角括弧+port):  '[2001:db8::1]:8080'   -> '2001:db8::1'
    - IPv6 (角括弧なし+port、CloudFront 非標準): '2001:db8::1:60776' -> '2001:db8::1'
    """
    value = value.strip()
    if not value:
        return None

    # 1) 角括弧つき IPv6: [addr]:port または [addr]
    if value.startswith("["):
        return _validated(value[1:].split("]", 1)[0])

    # 2) port 無しでそのまま妥当な IP ならそれを採用 (素の IPv4 / IPv6)
    direct = _validated(value)
    if direct is not None:
        return direct

    # 3) "最後のコロンの後ろ = port" とみなして左側を IP 候補にする
    #    (IPv4:port / 角括弧なし IPv6 + port をカバー)
    if ":" in value:
        return _validated(value.rsplit(":", 1)[0])
    return None


def _validated(addr: str) -> str | None:
    try:
        return str(ipaddress.ip_address(addr.strip()))
    except ValueError:
        return None
