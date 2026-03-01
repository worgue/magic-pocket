import pytest

from pocket.context import Context
from pocket.general_context import VpcContext
from pocket.general_settings import Vpc
from pocket.settings import Settings


def test_external_vpc_settings(use_toml):
    """manage=false の外部 VPC が正しくパースされる"""
    use_toml("tests/data/toml/rds_external_vpc.toml")
    settings = Settings.from_toml(stage="dev")
    assert settings.vpc is not None
    assert settings.vpc.ref == "main"
    assert settings.vpc.manage is False
    assert settings.vpc.zone_suffixes == []


def test_external_vpc_context(use_toml):
    """manage=false の外部 VPC コンテキストが正しく生成される"""
    use_toml("tests/data/toml/rds_external_vpc.toml")
    context = Context.from_toml(stage="dev")
    assert context.awscontainer is not None
    assert context.awscontainer.vpc is not None
    assert context.awscontainer.vpc.manage is False
    assert context.awscontainer.vpc.name == "main-pocket"


def test_managed_vpc_requires_zone_suffixes():
    """manage=true では zone_suffixes が必須"""
    with pytest.raises(ValueError, match="zone_suffixes is required when manage=true"):
        Vpc.model_validate({"ref": "main", "manage": True})


def test_unmanaged_vpc_no_zone_suffixes_required():
    """manage=false では zone_suffixes 不要"""
    vpc = Vpc.model_validate({"ref": "main", "manage": False})
    assert vpc.zone_suffixes == []
    assert vpc.manage is False


def test_unmanaged_vpc_cannot_be_sharable():
    """manage=false では sharable=true にできない"""
    with pytest.raises(ValueError, match="sharable requires manage=true"):
        Vpc.model_validate({"ref": "main", "manage": False, "sharable": True})


def test_unmanaged_vpc_cannot_have_efs():
    """manage=false では efs を設定できない"""
    with pytest.raises(ValueError, match="efs requires manage=true"):
        Vpc.model_validate({"ref": "main", "manage": False, "efs": {}})


def test_managed_vpc_sharable():
    """manage=true で sharable=true が設定可能"""
    vpc = Vpc.model_validate(
        {"ref": "main", "zone_suffixes": ["a", "c"], "sharable": True}
    )
    assert vpc.sharable is True
    assert vpc.manage is True


def test_vpc_name_format():
    """VPC 名が {ref}-{namespace} 形式であること"""
    from pocket.general_settings import GeneralSettings

    gs = GeneralSettings.model_validate(
        {
            "region": "ap-northeast-1",
            "project_name": "myprj",
            "stages": ["dev"],
        }
    )
    vpc = Vpc.model_validate({"ref": "main", "zone_suffixes": ["a", "c"]})
    ctx = VpcContext.from_settings(vpc, gs)
    assert ctx.name == "main-pocket"


def test_use_vpc_auto(use_toml):
    """use_vpc 未指定（auto）: [vpc] があれば自動的に VPC 内に配置"""
    use_toml("tests/data/toml/rds.toml")
    settings = Settings.from_toml(stage="dev")
    assert settings.awscontainer is not None
    assert settings.awscontainer.vpc is not None
    assert settings.rds is not None
    assert settings.rds.vpc is not None


def test_use_vpc_false():
    """use_vpc=false: VPC を使わない"""
    data = {
        "stage": "dev",
        "general": {
            "region": "ap-northeast-1",
            "project_name": "testprj",
            "stages": ["dev"],
        },
        "vpc": {"ref": "main", "zone_suffixes": ["a", "c"]},
        "awscontainer": {
            "dockerfile_path": "Dockerfile",
            "use_vpc": False,
        },
    }
    Settings.resolve_vpc(data)
    assert "vpc" not in data["awscontainer"]


def test_use_vpc_true_without_vpc_section():
    """use_vpc=true で [vpc] がない場合エラー"""
    data = {
        "stage": "dev",
        "general": {
            "region": "ap-northeast-1",
            "project_name": "testprj",
            "stages": ["dev"],
        },
        "awscontainer": {
            "dockerfile_path": "Dockerfile",
            "use_vpc": True,
        },
    }
    with pytest.raises(ValueError, match="use_vpc=true"):
        Settings.resolve_vpc(data)


def test_external_vpc_rds_no_zone_suffixes_check():
    """manage=false の外部 VPC では zone_suffixes のチェックが不要"""
    settings = Settings.model_validate(
        {
            "stage": "dev",
            "general": {
                "region": "ap-northeast-1",
                "project_name": "testprj",
                "stages": ["dev"],
            },
            "vpc": {"ref": "main", "manage": False},
            "rds": {"vpc": {"ref": "main", "manage": False}},
            "awscontainer": {
                "dockerfile_path": "Dockerfile",
                "vpc": {"ref": "main", "manage": False},
            },
        }
    )
    assert settings.rds is not None
    assert settings.rds.vpc is not None
    assert settings.rds.vpc.manage is False
