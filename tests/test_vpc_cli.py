"""stage ごとの VPC 宣言を resource コマンドでも解決する。"""

import pytest
from click.testing import CliRunner
from pocket_cli.cli.vpc_cli import get_vpc_resource, vpc


@pytest.fixture
def staged_vpc(tmp_path, use_toml, monkeypatch):
    monkeypatch.delenv("POCKET_DEPLOY_STAGE", raising=False)
    path = tmp_path / "pocket.toml"
    path.write_text("""[general]
region = "ap-northeast-1"
project_name = "testprj"
stages = ["live", "sandbox"]
[vpc]
ref = "shared"
zone_suffixes = ["a", "c"]
[live.vpc]
ref = "live-network"
nat_gateway = false
internet_gateway = false
""")
    use_toml(str(path))


def test_stage_vpc_merges_common_settings(staged_vpc):
    context = get_vpc_resource("live").context
    assert context.name == "live-network-pocket"
    assert context.zone_suffixes == ["a", "c"]
    assert context.nat_gateway is False
    assert get_vpc_resource().context.name == "shared-pocket"


def test_stage_only_vpc_without_container(tmp_path, use_toml):
    path = tmp_path / "pocket.toml"
    path.write_text("""[general]
region = "ap-northeast-1"
project_name = "testprj"
stages = ["live"]
[live.vpc]
ref = "retired"
zone_suffixes = ["a"]
""")
    use_toml(str(path))
    assert get_vpc_resource("live").context.name == "retired-pocket"


def test_yaml_uses_stage_override(staged_vpc):
    result = CliRunner().invoke(vpc, ["yaml", "--stage", "live"])
    assert result.exit_code == 0, result.output
    assert "live-network-pocket-vpc-id" in result.output
