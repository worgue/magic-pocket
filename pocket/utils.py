import importlib
import os
import sys
from functools import cache
from pathlib import Path

import boto3
from rich.console import Console
from rich.theme import Theme

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

# management_command_handler が「例外なく完了した」ときにだけ標準出力へ印字する
# センチネル。非同期 (InvocationType="Event") invoke ではハンドラの戻り値/例外が
# 呼び出し側に伝わらないため、CLI 側 (LambdaHandler.show_logs) はこの行が
# CloudWatch ログに現れたかどうかで成否を判定する。行が無いまま REPORT に達したら
# 失敗とみなして非ゼロ終了する (migrate 失敗が緑で通る "false green" を防ぐ)。
MANAGE_HANDLER_SUCCESS_SENTINEL = "POCKET_MANAGE_HANDLER_SUCCESS"

# pocket.runtime.toml の先頭に刻む「生成元 (CLI) バージョン」マーカー。
# TOML コメントとして書くので tomllib は無視する = 旧 runtime の後方互換を壊さない。
# 新しい runtime だけがこの行を読み、CLI 版 > 自身の runtime 版なら「古い runtime が
# 新しい runtime.toml を読んで INIT で opaque に落ちる」不整合を legible error に
# リフレーミングする (magic-pocket-cli と magic-pocket は lockstep リリース)。
GENERATOR_VERSION_MARKER = "# magic-pocket-cli generator version:"


def parse_generator_version(text: str) -> str | None:
    """runtime.toml 本文から生成元版マーカーコメントを取り出す。無ければ None。"""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(GENERATOR_VERSION_MARKER):
            return stripped[len(GENERATOR_VERSION_MARKER) :].strip() or None
    return None


def version_tuple(version: str) -> tuple[int, ...]:
    """ "0.10.0" → (0, 10, 0)。pre-release 等の suffix は各パートの先頭数字だけ採る。

    packaging に依存せず版境界を粗く比較するための最小実装 (lockstep なので粗くて十分)。
    """
    parts: list[int] = []
    for part in version.split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


# 診断・状態メッセージ (echo.*) はすべて stderr に出す。stdout は
# 「機械が食う値」(click.echo による URL / IAM Action 一覧 等) 専用とし、
# `$(pocket resource neon url ...)` のような capture を診断ログで汚さない。
# markup=False: echo は任意の診断文字列 (パスや `magic-pocket[django]` のような
# extras 表記を含む) を渡すので、`[...]` を Rich markup として解釈させない
# (解釈すると角括弧内が style タグ扱いで消える footgun)。
_console = Console(
    stderr=True,
    markup=False,
    theme=Theme(
        {
            "success": "green",
            "info": "cyan",
            "warning": "magenta",
            "danger": "bold red",
            "log": "dim",
        }
    ),
)


class Echo:
    def success(self, message):
        _console.print(message, style="success")

    def info(self, message):
        _console.print(message, style="info")

    def warning(self, message):
        _console.print(message, style="warning")

    def danger(self, message):
        _console.print(message, style="danger")

    def log(self, message):
        _console.print(message, style="log")


echo = Echo()


def get_stage():
    return os.environ.get("POCKET_STAGE") or "__none__"


def _find_pyproject_dir() -> Path:
    """pyproject.toml を CWD から上方向に探索し、見つかったディレクトリを返す。"""
    current = Path.cwd().resolve()
    while True:
        if (current / "pyproject.toml").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return Path.cwd()


def is_runtime() -> bool:
    """アプリケーションのランタイム環境（Lambda 等）で動作しているかを判定する。

    現在は AWS Lambda のみ対応。将来 ECS 等を追加する場合はここを拡張する。
    """
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def _find_file_upward(filename: str) -> Path | None:
    """CWD から上方向にファイルを探索し、見つかったパスを返す。"""
    current = Path.cwd().resolve()
    while True:
        candidate = current / filename
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def get_toml_path() -> Path:
    """pocket.toml のパスを返す。

    ランタイム環境では pocket.runtime.toml を優先する。
    CLI 実行時（pocket deploy 等）では常に pocket.toml を返す。
    いずれも CWD から上方向に探索する。
    """
    if is_runtime():
        runtime_toml = _find_file_upward("pocket.runtime.toml")
        if runtime_toml:
            return runtime_toml
    toml = _find_file_upward("pocket.toml")
    if toml:
        return toml
    # フォールバック: pyproject.toml のディレクトリ
    return _find_pyproject_dir() / "pocket.toml"


def get_project_name():
    pyproject = _find_pyproject_dir() / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    if data.get("project", {}).get("name"):
        return data["project"]["name"]
    return Path.cwd().name


@cache
def get_hosted_zones():
    echo.log("Requesting Route53 hosted zone list...")
    res = boto3.client("route53").list_hosted_zones()
    if res["IsTruncated"]:
        raise Exception(
            "Route53 hosted zone list is truncated. Please set hosted_zone_id."
        )
    return res["HostedZones"]


@cache
def get_hosted_zone_id_from_domain(domain: str):
    echo.log("Searching hostedzone_id from domain: %s" % domain)
    zone_matched = [
        zone for zone in get_hosted_zones() if zone["Name"].strip(".") in domain
    ]
    if len(zone_matched) == 0:
        raise Exception(
            "No route53 hosted zone for the domain. [%s]\n"
            "Check your route53 hosted zone or set hosted_zone_id in pocket.toml"
            % domain
        )
    best_match = sorted(zone_matched, key=lambda z: len(z["Name"]), reverse=True)[0]
    best_match_id = best_match["Id"][len("/hostedzone/") :]
    echo.log("Found hostedzone")
    echo.log("  Name: %s" % best_match["Name"])
    echo.log("  Id: %s" % best_match_id)
    return best_match_id


def get_wsgi_application():
    try:
        mod = importlib.import_module("%s.wsgi" % get_project_name())
    except ModuleNotFoundError:
        print("Failed to import WSGI application %s.wsgi" % get_project_name())
        raise
    return mod.application
