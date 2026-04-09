import json
import os

import boto3
import pytest
from moto import mock_aws
from pocket_cli.resources.rds import Rds

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


def test_rds_snapshot_identifier_field():
    """snapshot_identifier が settings/context に正しく伝搬する"""
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
                "snapshot_identifier": "signage-prod-20260410",
            },
            "awscontainer": {
                "dockerfile_path": "Dockerfile",
                "vpc": {"ref": "main", "zone_suffixes": ["a", "c"]},
            },
        }
    )
    assert settings.rds is not None
    assert settings.rds.snapshot_identifier == "signage-prod-20260410"
    context = Context.from_settings(settings)
    assert context.rds is not None
    assert context.rds.snapshot_identifier == "signage-prod-20260410"


def test_rds_snapshot_identifier_default_none():
    """snapshot_identifier 未指定時は None"""
    settings = Settings.model_validate(
        {
            "stage": "dev",
            "general": {
                "region": "ap-northeast-1",
                "project_name": "testprj",
                "stages": ["dev"],
            },
            "vpc": {"ref": "main", "zone_suffixes": ["a", "c"]},
            "rds": {"vpc": {"ref": "main", "zone_suffixes": ["a", "c"]}},
            "awscontainer": {
                "dockerfile_path": "Dockerfile",
                "vpc": {"ref": "main", "zone_suffixes": ["a", "c"]},
            },
        }
    )
    assert settings.rds is not None
    assert settings.rds.snapshot_identifier is None


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


# --- moto テスト ---


@mock_aws
def test_rds_master_user_secret_properties(use_toml):
    """RDS クラスター作成後に secret_arn, kms_key_id, endpoint, port が取得できる"""
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.rds is not None

    from pocket_cli.resources.vpc import Vpc

    vpc = Vpc(context.rds.vpc)
    vpc.create()

    rds = Rds(context.rds)
    rds.create()

    assert rds.master_user_secret_arn is not None
    assert rds.master_user_secret_kms_key_id is not None
    assert rds.endpoint is not None
    assert rds.port is not None
    assert rds.database_name == context.rds.database_name


@mock_aws
def test_rds_secret_lacks_host(use_toml):
    """ManageMasterUserPassword のシークレットに host/port が含まれないことを確認"""
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.rds is not None

    from pocket_cli.resources.vpc import Vpc

    vpc = Vpc(context.rds.vpc)
    vpc.create()

    rds = Rds(context.rds)
    rds.create()

    sm = boto3.client("secretsmanager", region_name=context.rds.region)
    secret = sm.get_secret_value(SecretId=rds.master_user_secret_arn)
    data = json.loads(secret["SecretString"])

    assert "username" in data
    assert "password" in data
    assert "host" not in data
    assert "port" not in data


@mock_aws
def test_set_rds_database_url_with_env_fallback(use_toml):
    """_set_rds_database_url がシークレットに host がない場合に環境変数で補完する"""
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.rds is not None

    from pocket_cli.resources.vpc import Vpc

    vpc = Vpc(context.rds.vpc)
    vpc.create()

    rds = Rds(context.rds)
    rds.create()

    os.environ["POCKET_RDS_SECRET_ARN"] = rds.master_user_secret_arn
    os.environ["POCKET_RDS_ENDPOINT"] = rds.endpoint
    os.environ["POCKET_RDS_PORT"] = str(rds.port)
    os.environ["POCKET_RDS_DBNAME"] = rds.database_name

    try:
        from pocket.runtime import _set_rds_database_url

        _set_rds_database_url()

        database_url = os.environ.get("DATABASE_URL", "")
        assert database_url.startswith("postgres://")
        assert rds.endpoint in database_url
        assert str(rds.port) in database_url
        assert rds.database_name in database_url
    finally:
        os.environ.pop("POCKET_RDS_SECRET_ARN", None)
        os.environ.pop("POCKET_RDS_ENDPOINT", None)
        os.environ.pop("POCKET_RDS_PORT", None)
        os.environ.pop("POCKET_RDS_DBNAME", None)
        os.environ.pop("DATABASE_URL", None)
