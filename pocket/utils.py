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

_console = Console(
    theme=Theme(
        {
            "success": "green",
            "info": "cyan",
            "warning": "magenta",
            "danger": "bold red",
            "log": "dim",
        }
    )
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


def get_toml_path() -> Path:
    """pocket.toml のパスを返す。pyproject.toml と同じディレクトリにある前提。"""
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
