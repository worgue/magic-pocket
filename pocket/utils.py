import importlib
import os
import re
import sys
from functools import cache
from pathlib import Path
from typing import Any

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

# context / settings の表示用 dump で値を伏せるキー名。api_key (Neon/Upstash)、
# private_key (TiDB)、token_secret (CloudFront signed URL)、password
# (basic_auth_credential options) 等、pocket.toml / env 由来の credential が対象。
# 値の取得経路は url 系コマンドや SSM が担うため、表示系では常にマスクする。
SECRET_DUMP_KEYS = frozenset(
    {"api_key", "private_key", "public_key", "token_secret", "password"}
)


def mask_secret_values(data: Any) -> Any:
    """表示用 dump (dict/list) の secret キーの値を "****" に置換して返す。

    None は「未設定」の情報として残し、値がある場合のみマスクする。
    """
    if isinstance(data, dict):
        return {
            k: (
                "****"
                if k in SECRET_DUMP_KEYS and v is not None
                else mask_secret_values(v)
            )
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [mask_secret_values(v) for v in data]
    return data


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

    def _zone_matches(zone_name: str) -> bool:
        # ラベル境界付き suffix 判定。substring 判定だと example.com zone が
        # badexample.com (別ドメイン) に誤マッチする
        name = zone_name.strip(".")
        return domain == name or domain.endswith("." + name)

    zone_matched = [zone for zone in get_hosted_zones() if _zone_matches(zone["Name"])]
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


def route_logical_name(path_pattern: str) -> str:
    """route の path_pattern から CFn 論理 ID / Origin Id 用の名前を導出する。

    context.RouteContext.name と settings の一意性検証が同じ導出を共有する
    (片方だけ変えると衝突検査がすり抜ける)。
    """
    if not path_pattern:
        return "root"
    parts = []
    for part in path_pattern.split("/"):
        if part and part != "*":
            alnum_only = "".join(ch for ch in part if ch.isalnum())
            parts.append(alnum_only)
    return "-".join(parts)


def camel_logical_name(key: str) -> str:
    """非英数字を境界にした CamelCase の CFn 論理 ID を導出する。"""
    return "".join(
        part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", key) if part
    )
