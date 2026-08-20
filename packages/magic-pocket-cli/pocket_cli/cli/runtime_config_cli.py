from __future__ import annotations

import copy
import json
import re
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import click

from pocket.utils import GENERATOR_VERSION_MARKER, get_toml_path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

# container.<name> から除外するキー（ビルド時のみ必要）
# iam (managed_policy_arns / inline_policies) は Lambda execution role を組む
# provision 専用で runtime は参照しない。IAM Condition のようにキーへ ":" を含む
# 任意の dict を抱えるため、runtime toml の表面積から外しておく。
_CONTAINER_REMOVE_KEYS = {
    "platform",
    "build",
    "permissions_boundary",
    "use_vpc",
    "iam",
}

# container.<name> でダミー値に置き換えるキー（必須フィールドだが runtime では不要）
_CONTAINER_DUMMY_VALUES = {
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
    "upload_dir",
    "require_token",
    "login_path",
}

# container.<name>.django から除外するキー
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
    """container / cloudfront セクションをクリーンアップする"""
    containers = section.get("container")
    if isinstance(containers, dict):
        for c in containers.values():
            if not isinstance(c, dict):
                continue
            _remove_keys(c, _CONTAINER_REMOVE_KEYS)
            for key, value in _CONTAINER_DUMMY_VALUES.items():
                if key in c:
                    c[key] = value
            if "django" in c:
                _remove_keys(c["django"], _DJANGO_REMOVE_KEYS)
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
        section = f"{prefix}{_toml_key(key)}" if prefix else _toml_key(key)
        # dict の中身が全て dict なら、各サブキーをサブセクションに
        if all(isinstance(v, dict) for v in value.values()) and value:
            for sub_key, sub_value in value.items():
                sub_section = f"{section}.{_toml_key(sub_key)}"
                lines.append("")
                lines.append(f"[{sub_section}]")
                lines.append(_to_toml(sub_value, prefix=f"{sub_section}."))
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


_BARE_KEY_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")


def _toml_key(key: str) -> str:
    """TOML のキーとして安全な表現を返す。

    bare key に使える文字は `[A-Za-z0-9_-]` だけ。IAM Condition の
    `"kms:ViaService"` のように ":" や "." を含むキーをそのまま書くと不正 TOML に
    なり、image に焼き込まれて Lambda INIT で初めて落ちる (値側の _toml_string と
    同じ事故がキー側で起きる)。dotted なセクション名は各パートを個別に通すこと。
    """
    if _BARE_KEY_RE.match(key):
        return key
    return _toml_string(key)


def _format_value(key: str, value) -> str:
    if isinstance(value, list):
        return f"{_toml_key(key)} = {_format_list(value)}"
    return f"{_toml_key(key)} = {_format_inline_value(value)}"


def _format_list(items: list) -> str:
    if not items:
        return "[]"
    if all(isinstance(i, dict) for i in items):
        parts = [_format_inline_table(item) for item in items]
        return "[\n    %s,\n]" % ",\n    ".join(parts)
    return "[%s]" % ", ".join(_format_inline_value(i) for i in items)


def _format_inline_table(table: dict) -> str:
    if not table:
        return "{}"
    kvs = ", ".join(
        f"{_toml_key(k)} = {_format_inline_value(v)}" for k, v in table.items()
    )
    return "{ %s }" % kvs


def _format_inline_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _toml_string(value)
    # inline table / 配列は再帰。IAM の Condition のようにネストした dict を
    # repr() で吐くと Python 表記 ({'k': 'v'}) になり不正 TOML になる。
    if isinstance(value, dict):
        return _format_inline_table(value)
    if isinstance(value, list):
        return _format_list(value)
    return repr(value)


def _generator_version() -> str | None:
    """生成元 (magic-pocket-cli) のバージョン。取得できなければ None (刻印しない)。"""
    try:
        return version("magic-pocket-cli")
    except PackageNotFoundError:
        return None


class RuntimeConfigGenerationError(Exception):
    """生成した pocket.runtime.toml が不正な TOML だった。"""


def _check_valid_toml(toml_str: str) -> None:
    """生成物を自己検証する (fail-loud)。

    自前 dumper が取りこぼした値/キーがあっても、ここで落ちれば build/deploy 時に
    分かる。素通しすると不正な toml が image に焼かれ、Lambda INIT の
    TOMLDecodeError として全 handler が死ぬまで気づけない。
    """
    try:
        tomllib.loads(toml_str)
    except tomllib.TOMLDecodeError as e:
        raise RuntimeConfigGenerationError(
            "生成した pocket.runtime.toml が不正な TOML です: %s\n"
            "pocket.toml の値/キーが自前 dumper の想定外の可能性があります。\n"
            "--- 生成結果 ---\n%s" % (e, toml_str)
        ) from e


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
    _check_valid_toml(toml_str)
    return toml_str


def generate_runtime_config(output_path: Path) -> None:
    """pocket.runtime.toml を生成する（プログラムから呼び出し用）

    runtime が INIT で読むファイルのため、strict な umask 環境でも
    other-read を保証する (context_check がエラーにする条件を自ら作らない)。
    """
    output_path.write_text(_runtime_config_str())
    output_path.chmod(0o644)


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
