"""pocket.toml の相対パスとビルドコンテキストの基準が pocket.toml のディレクトリ。

pocket.toml は cwd から上方向に探すので、下位ディレクトリ (例: `frontend/`) からでも
設定は読める。そのとき build context / `dockerfile_path` / route の `build` と
`upload_dir` / `managed_assets` / django の `project_dir` が cwd 基準だと、別の
ディレクトリを context にしたり `open Dockerfile: no such file or directory` で
落ちたりする。ここでは cwd を下位ディレクトリにして基準を確かめる。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from pocket_cli.paths import project_dir, project_path
from pocket_cli.resources.aws.builders import depot as depot_module
from pocket_cli.resources.aws.builders import docker as docker_module
from pocket_cli.resources.aws.builders.depot import DepotBuilder
from pocket_cli.resources.aws.builders.docker import DockerBuilder
from pocket_cli.resources.aws.ecr import Ecr
from pocket_cli.resources.cloudfront import CloudFront

from pocket.context import Context

REGION = "ap-northeast-1"

TOML = """
[general]
region = "ap-northeast-1"
project_name = "paths"
stages = ["dev"]

[s3]

[cloudfront.web]
managed_assets = "assets"

[[cloudfront.web.routes]]
is_default = true
origin_path = "/spa"
build = { dir = "out", cmd = "mkdir -p out && touch out/index.html" }
"""


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    """pocket.toml のある project に、cwd を下位ディレクトリ frontend/ にして入る"""
    root = tmp_path / "prj"
    (root / "frontend").mkdir(parents=True)
    (root / "pocket.toml").write_text(TOML)
    (root / "pyproject.toml").write_text('[project]\nname = "paths"\n')
    (root / "Dockerfile").write_text("FROM scratch\n")
    monkeypatch.chdir(root / "frontend")
    return root


def test_project_dir_is_toml_dir(project):
    assert project_dir() == project.resolve()
    assert project_path("Dockerfile") == project.resolve() / "Dockerfile"
    assert project_path("/abs/x") == Path("/abs/x")


class _RecordingBuilder:
    def __init__(self):
        self.calls: list[dict] = []

    def build_and_push(self, **kwargs):
        self.calls.append(kwargs)

    def delete(self):
        pass


@mock_aws
def test_ecr_passes_toml_dir_as_context(project):
    boto3.client("ecr", region_name=REGION).create_repository(repositoryName="repo")
    builder = _RecordingBuilder()
    ecr = Ecr(REGION, "repo", "dev", "Dockerfile", "linux/amd64", builder=builder)
    ecr.build_and_push()
    assert builder.calls[0]["context_dir"] == project.resolve()
    assert builder.calls[0]["dockerfile_path"] == "Dockerfile"


def test_docker_builder_uses_context_dir(project, monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(docker_module.docker, "login_ecr", lambda **kw: None)
    monkeypatch.setattr(
        docker_module.docker, "build", lambda *a, **kw: calls.update(args=a, kw=kw)
    )
    DockerBuilder(region=REGION).build_and_push(
        target="t",
        context_dir=project.resolve(),
        dockerfile_path="Dockerfile",
        platform="linux/amd64",
    )
    assert calls["args"] == (str(project.resolve()),)
    assert calls["kw"]["file"] == str(project.resolve() / "Dockerfile")


def test_depot_builder_uses_context_dir(project, monkeypatch):
    commands: list = []
    monkeypatch.setenv("DEPOT_TOKEN", "x")
    monkeypatch.setattr(depot_module.docker, "login_ecr", lambda **kw: None)
    monkeypatch.setattr(
        depot_module.subprocess, "run", lambda cmd, **kw: commands.append(cmd)
    )
    DepotBuilder(region=REGION).build_and_push(
        target="t",
        context_dir=project.resolve(),
        dockerfile_path="Dockerfile",
        platform="linux/amd64",
    )
    cmd = commands[0]
    assert cmd[2] == str(project.resolve())
    assert cmd[cmd.index("--file") + 1] == str(project.resolve() / "Dockerfile")


def test_route_build_and_upload_dir_are_toml_relative(project, monkeypatch):
    context = Context.from_toml(stage="dev")
    cf_ctx = context.cloudfront["web"]
    route = cf_ctx.routes[0]
    # build.cmd は pocket.toml のディレクトリで実行する
    runs: list = []
    real_run = subprocess.run

    def recording_run(cmd, **kw):
        runs.append(kw.get("cwd"))
        return real_run(cmd, **kw)

    monkeypatch.setattr("pocket_cli.resources.cloudfront.subprocess.run", recording_run)
    cf = CloudFront(cf_ctx)
    # S3 への同期と invalidation は対象外 (build の cwd と upload_dir の解決だけを見る)
    monkeypatch.setattr(cf, "_upload_route", lambda r: False)
    monkeypatch.setattr(cf, "_invalidate", lambda routes: None)
    cf.upload(routes=[route])
    assert runs[0] == project.resolve()
    assert (project / "out/index.html").is_file()
    files = CloudFront._local_files(route, "spa")
    assert files == {"spa/index.html": project.resolve() / "out/index.html"}


def test_managed_asset_files_are_toml_relative(project):
    (project / "assets/default").mkdir(parents=True)
    (project / "assets/default/robots.txt").write_text("")
    context = Context.from_toml(stage="dev")
    assert context.cloudfront["web"].managed_asset_files == ["robots.txt"]
