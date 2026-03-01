import pytest

from pocket.context import Context
from pocket.settings import Settings


def test_rds_settings_from_toml(use_toml):
    use_toml("tests/data/toml/rds.toml")
    settings = Settings.from_toml(stage="dev")
    assert settings.rds is not None
    assert settings.rds.vpc is not None
    assert settings.rds.vpc.ref == "main"
    assert settings.rds.min_capacity == 0.5
    assert settings.rds.max_capacity == 2.0


def test_rds_context(use_toml):
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.rds is not None
    assert context.rds.cluster_identifier == "dev-testprj-pocket-aurora"
    assert context.rds.instance_identifier == "dev-testprj-pocket-aurora-1"
    assert context.rds.database_name == "testprj_dev"
    assert context.rds.master_username == "postgres"
    assert context.rds.subnet_group_name == "dev-testprj-pocket-aurora"
    assert context.rds.security_group_name == "dev-testprj-pocket-aurora-rds"
    assert context.rds.region == "ap-northeast-1"
    assert context.rds.vpc.ref == "main"
    assert len(context.rds.vpc.zone_suffixes) == 2


def test_rds_requires_2_azs_when_managed():
    """managed VPC では RDS に最低 2 AZ 必要"""
    with pytest.raises(ValueError, match="at least 2 zone_suffixes"):
        Settings.model_validate(
            {
                "stage": "dev",
                "general": {
                    "region": "ap-northeast-1",
                    "project_name": "testprj",
                    "stages": ["dev"],
                },
                "vpc": {"ref": "main", "zone_suffixes": ["a"]},
                "rds": {"vpc": {"ref": "main", "zone_suffixes": ["a"]}},
                "awscontainer": {
                    "dockerfile_path": "Dockerfile",
                    "vpc": {"ref": "main", "zone_suffixes": ["a"]},
                },
            }
        )


def test_rds_requires_awscontainer_vpc():
    """RDS には awscontainer + vpc が必須"""
    with pytest.raises(ValueError, match="rds requires awscontainer with VPC"):
        Settings.model_validate(
            {
                "stage": "dev",
                "general": {
                    "region": "ap-northeast-1",
                    "project_name": "testprj",
                    "stages": ["dev"],
                },
                "vpc": {"ref": "main", "zone_suffixes": ["a", "c"]},
                "rds": {"vpc": {"ref": "main", "zone_suffixes": ["a", "c"]}},
            }
        )


def test_rds_none_when_not_configured(use_toml):
    """RDS 未設定時は None"""
    use_toml("tests/data/toml/default.toml")
    context = Context.from_toml(stage="dev")
    assert context.rds is None


def test_rds_custom_capacity():
    """カスタム min/max capacity の設定"""
    settings = Settings.model_validate(
        {
            "stage": "dev",
            "general": {
                "region": "ap-northeast-1",
                "project_name": "testprj",
                "stages": ["dev"],
            },
            "vpc": {"ref": "main", "zone_suffixes": ["a", "c"]},
            "rds": {
                "vpc": {"ref": "main", "zone_suffixes": ["a", "c"]},
                "min_capacity": 1.0,
                "max_capacity": 4.0,
            },
            "awscontainer": {
                "dockerfile_path": "Dockerfile",
                "vpc": {"ref": "main", "zone_suffixes": ["a", "c"]},
            },
        }
    )
    assert settings.rds is not None
    assert settings.rds.min_capacity == 1.0
    assert settings.rds.max_capacity == 4.0


def test_rds_resolve_vpc(use_toml):
    """resolve_vpc が rds の vpc を正しく解決する"""
    use_toml("tests/data/toml/rds.toml")
    settings = Settings.from_toml(stage="dev")
    assert settings.rds is not None
    assert settings.rds.vpc is not None
    assert settings.rds.vpc.ref == "main"
    assert settings.rds.vpc.zone_suffixes == ["a", "c"]


def test_rds_check_keys(use_toml):
    """check_keys に rds が含まれること"""
    use_toml("tests/data/toml/rds.toml")
    # エラーなく from_toml できること
    settings = Settings.from_toml(stage="dev")
    assert settings.rds is not None
