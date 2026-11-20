"""resource cloudfront / cloudfront-keys の status・yaml は secret を読んでから描く。

secret (origin verify の値・署名の公開鍵) を読まずに template を描くと、deploy 済みの
template hash と一致せず、毎回 REQUIRE_UPDATE になる (`pocket status` とずれる)。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner
from pocket_cli.cli import cloudfront_cli, cloudfront_keys_cli


class _FakeEdge:
    def __init__(self, events: list[str]):
        self.events = events
        self.context = SimpleNamespace(name="web")

    def prepare_deploy(self, mediator):
        assert mediator == "mediator"
        self.events.append("prepare")

    @property
    def status(self):
        self.events.append("status")
        return "COMPLETED"

    @property
    def stack(self):
        self.events.append("yaml")
        return SimpleNamespace(yaml="Resources: {}")


@pytest.mark.parametrize(
    ("module", "group", "getter"),
    [
        (cloudfront_cli, cloudfront_cli.cloudfront, "get_cloudfront_resources"),
        (
            cloudfront_keys_cli,
            cloudfront_keys_cli.cloudfront_keys,
            "get_cloudfront_keys_resources",
        ),
    ],
)
@pytest.mark.parametrize("command", ["status", "yaml"])
def test_prepare_before_render(monkeypatch, module, group, getter, command):
    events: list[str] = []
    contexts: list[str] = []

    def fake_from_toml(stage):
        contexts.append(stage)
        return "context"

    monkeypatch.setattr(module.Context, "from_toml", fake_from_toml)
    monkeypatch.setattr(module, "Mediator", lambda context: "mediator")
    monkeypatch.setattr(
        module, getter, lambda stage, name, context: [_FakeEdge(events)]
    )
    result = CliRunner().invoke(group, [command, "--stage", "dev"])
    assert result.exit_code == 0, result.output
    assert events == ["prepare", "status" if command == "status" else "yaml"]
    # Context は 1 回だけ作る
    assert contexts == ["dev"]


@pytest.mark.parametrize("command", ["create", "update"])
def test_cloudfront_create_update_pass_mediator(monkeypatch, command):
    """mediator を渡さないと origin verify 等の secret が template に入らない"""
    received: list[object] = []

    class _Fake:
        context = SimpleNamespace(name="web")
        status = "COMPLETED"

        def create(self, mediator=None):
            received.append(mediator)

        def update(self, mediator=None):
            received.append(mediator)

    monkeypatch.setattr(cloudfront_cli.Context, "from_toml", lambda stage: "context")
    monkeypatch.setattr(cloudfront_cli, "Mediator", lambda context: "mediator")
    monkeypatch.setattr(
        cloudfront_cli, "get_cloudfront_resources", lambda s, n, c: [_Fake()]
    )
    result = CliRunner().invoke(cloudfront_cli.cloudfront, [command, "--stage", "dev"])
    assert result.exit_code == 0, result.output
    assert received == ["mediator"]


def test_local_files_requires_upload_dir(tmp_path, monkeypatch):
    """無い upload_dir を空として扱うと、prune / delete_stale が全 key を消す"""
    from pocket_cli.resources import cloudfront as cloudfront_resource

    monkeypatch.setattr(
        cloudfront_resource, "project_path", lambda path: tmp_path / path
    )
    route: Any = SimpleNamespace(upload_dir="build/assets")
    with pytest.raises(RuntimeError, match="build の出力先を確認してください"):
        cloudfront_resource.CloudFront._local_files(route, "assets")
    (tmp_path / "build/assets").mkdir(parents=True)
    (tmp_path / "build/assets/a.js").write_text("x")
    files = cloudfront_resource.CloudFront._local_files(route, "assets")
    assert list(files) == ["assets/a.js"]
