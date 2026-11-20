"""resource cloudfront / cloudfront-keys の status・yaml は secret を読んでから描く。

secret (origin verify の値・署名の公開鍵) を読まずに template を描くと、deploy 済みの
template hash と一致せず、毎回 REQUIRE_UPDATE になる (`pocket status` とずれる)。
"""

from __future__ import annotations

from types import SimpleNamespace

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
