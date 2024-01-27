import sys
from pathlib import Path

import boto3

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib
from rich.console import Console
from rich.theme import Theme

console = Console(
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


def get_default_region():
    return boto3.Session().region_name


def get_project_name():
    data = tomllib.loads(Path("pyproject.toml").read_text())
    if data.get("project", {}).get("name"):
        return data["project"]["name"]
    return Path.cwd().name


def get_hosted_zone_id_from_domain(domain: str):
    console.print("Searching hostedzone_id from domain: %s" % domain, style="log")
    console.print("Requesting Route53 hosted zone list...", style="log")
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
    console.print("Found hostedzone", style="log")
    console.print("  Name: %s" % best_match["Name"], style="log")
    console.print("  Id: %s" % best_match_id, style="log")
    return best_match_id
