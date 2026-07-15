from __future__ import annotations

import copy
import json
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import click

from pocket.utils import GENERATOR_VERSION_MARKER, get_toml_path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

# awscontainer から除外するキー（ビルド時のみ必要）
_AWSCONTAINER_REMOVE_KEYS = {
    "platform",
    "build",
    "permissions_boundary",
    "use_vpc",
}

# awscontainer でダミー値に置き換えるキー（必須フィールドだが runtime では不要）
_AWSCONTAINER_DUMMY_VALUES = {
    "dockerfile_path": "__runtime__",
}

# cloudfront の各エントリから除外するキー
_CLOUDFRONT_REMOVE_KEYS = {
    "managed_assets",
    "hosted_zone_id_override",
    "redirect_from",
    "signing_key",
    "token_secret",
}

# route から除外するキー
_ROUTE_REMOVE_KEYS = {
    "build",
    "build_dir",
    "require_token",
    "login_path",
}

# awscontainer.django から除外するキー
_DJANGO_REMOVE_KEYS = {
    "project_dir",
}

# トップレベルから除外するセクション
_TOPLEVEL_REMOVE_KEYS = {
    "vpc",
}


def _remove_keys(d: dict, keys: set[str]) -> None:
    for key in keys:
        d.pop(key, None)


def _clean_cloudfront(cf: dict) -> None:
    _remove_keys(cf, _CLOUDFRONT_REMOVE_KEYS)
    for route in cf.get("routes", []):
        _remove_keys(route, _ROUTE_REMOVE_KEYS)


def _clean_section(section: dict) -> None:
    """awscontainer / cloudfront セクションをクリーンアップする"""
    if "awscontainer" in section:
        ac = section["awscontainer"]
        _remove_keys(ac, _AWSCONTAINER_REMOVE_KEYS)
        for key, value in _AWSCONTAINER_DUMMY_VALUES.items():
            if key in ac:
                ac[key] = value
        if "django" in ac:
            _remove_keys(ac["django"], _DJANGO_REMOVE_KEYS)
    if "cloudfront" in section:
        for cf in section["cloudfront"].values():
            _clean_cloudfront(cf)


def _clean_data(data: dict) -> dict:
    """pocket.toml のデータからランタイムに不要な設定を除外する"""
    result = copy.deepcopy(data)
    _remove_keys(result, _TOPLEVEL_REMOVE_KEYS)
    _clean_section(result)
    for stage in result.get("general", {}).get("stages", []):
        if stage in result:
            _clean_section(result[stage])
    return result


def _to_toml(data: dict, prefix: str = "") -> str:
    """dict を TOML 文字列に変換する（簡易実装）"""
    lines: list[str] = []
    # まずスカラー値とリストを出力
    for key, value in data.items():
        if isinstance(value, dict):
            continue
        lines.append(_format_value(key, value))

    # dict 値をセクションとして出力
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        section = f"{prefix}{key}" if prefix else key
        # dict の中身が全て dict なら、各サブキーをサブセクションに
        if all(isinstance(v, dict) for v in value.values()) and value:
            for sub_key, sub_value in value.items():
                lines.append("")
                lines.append(f"[{section}.{sub_key}]")
                lines.append(_to_toml(sub_value, prefix=f"{section}.{sub_key}."))
            continue
        lines.append("")
        lines.append(f"[{section}]")
        lines.append(_to_toml(value, prefix=f"{section}."))

    return "\n".join(lines)


def _toml_string(value: str) -> str:
    """TOML basic string として安全な表現を返す。

    エスケープ規則 (\" / \\\\ / 制御文字) は JSON と互換。単純連結だと
    引用符やバックスラッシュを含む値で不正 TOML になり、image に焼き込まれて
    Lambda INIT で初めて落ちる。
    """
    return json.dumps(value, ensure_ascii=False)


def _format_value(key: str, value) -> str:
    if isinstance(value, bool):
        return f"{key} = {'true' if value else 'false'}"
    if isinstance(value, int):
        return f"{key} = {value}"
    if isinstance(value, str):
        return f"{key} = {_toml_string(value)}"
    if isinstance(value, list):
        return f"{key} = {_format_list(value)}"
    return f"{key} = {value!r}"


def _format_list(items: list) -> str:
    if not items:
        return "[]"
    if all(isinstance(i, str) for i in items):
        return "[%s]" % ", ".join(_toml_string(i) for i in items)
    if all(isinstance(i, dict) for i in items):
        parts = []
        for item in items:
            kvs = ", ".join(f"{k} = {_format_inline_value(v)}" for k, v in item.items())
            parts.append("{ %s }" % kvs)
        return "[\n    %s,\n]" % ",\n    ".join(parts)
    return repr(items)


def _format_inline_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _toml_string(value)
    return repr(value)


def _generator_version() -> str | None:
    """生成元 (magic-pocket-cli) のバージョン。取得できなければ None (刻印しない)。"""
    try:
        return version("magic-pocket-cli")
    except PackageNotFoundError:
        return None


def _runtime_config_str() -> str:
    """pocket.runtime.toml の内容を生成する (stdout / ファイル出力の共通実装)。

    生成元 (CLI) バージョンを先頭コメントに刻む。旧 runtime (tomllib) は無視するので
    後方互換を壊さず、新 runtime だけが読んで版突合 (Settings.check_generator_version)
    に使う。CLI 版 > runtime 版のとき legible error にリフレーミングされる (層2)。
    stdout 経由 (`pocket runtime-config > pocket.runtime.toml`) でもマーカーが
    欠けないよう、生成は本関数に一本化する。
    """
    toml_path = get_toml_path()
    data = tomllib.loads(toml_path.read_text())
    cleaned = _clean_data(data)
    toml_str = _to_toml(cleaned).strip() + "\n"
    generator_version = _generator_version()
    if generator_version:
        toml_str = "%s %s\n%s" % (GENERATOR_VERSION_MARKER, generator_version, toml_str)
    return toml_str


def generate_runtime_config(output_path: Path) -> None:
    """pocket.runtime.toml を生成する（プログラムから呼び出し用）"""
    output_path.write_text(_runtime_config_str())


@click.command("runtime-config")
@click.argument("output", default="-")
def runtime_config(output: str):
    """Lambda ランタイム用の pocket.toml を生成する

    ビルド時のみ必要な設定（dockerfile_path, managed_assets 等）を
    除外した pocket.toml を出力する。Dockerfile 内で使用する。

    OUTPUT: 出力先ファイルパス（省略時は標準出力）
    """
    if output == "-":
        click.echo(_runtime_config_str(), nl=False)
    else:
        generate_runtime_config(Path(output))
        click.echo("runtime-config を出力しました: %s" % output)
