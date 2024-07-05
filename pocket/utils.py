import importlib
import os
import sys
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


def get_toml_path():
    pathname = os.environ.get("POCKET_TOML_PATH") or "pocket.toml"
    return Path(pathname)


def get_project_name():
    data = tomllib.loads(Path("pyproject.toml").read_text())
    if data.get("project", {}).get("name"):
        return data["project"]["name"]
    return Path.cwd().name


def get_hosted_zone_id_from_domain(domain: str):
    echo.log("Searching hostedzone_id from domain: %s" % domain)
    echo.log("Requesting Route53 hosted zone list...")
    res = boto3.client("route53").list_hosted_zones()
    if res["IsTruncated"]:
        raise Exception(
            "Route53 hosted zone list is truncated. Please set hosted_zone_id."
        )
    zone_matched = [
        zone for zone in res["HostedZones"] if zone["Name"].strip(".") in domain
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
