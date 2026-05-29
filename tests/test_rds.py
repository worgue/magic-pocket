import json
import os

import boto3
import pytest
from moto import mock_aws
from pocket_cli.resources.rds import Rds

from pocket.context import Context
from pocket.settings import Rds as RdsSettings
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
                "snapshot_identifier": "myapp-prod-20260410",
            },
            "awscontainer": {
                "dockerfile_path": "Dockerfile",
                "vpc": {"ref": "main", "zone_suffixes": ["a", "c"]},
            },
        }
    )
    assert settings.rds is not None
    assert settings.rds.snapshot_identifier == "myapp-prod-20260410"
    context = Context.from_settings(settings)
    assert context.rds is not None
    assert context.rds.snapshot_identifier == "myapp-prod-20260410"


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


def test_rds_unmanaged_mode():
    """managed = false で secret_arn / security_group_id を指定できること"""
    settings = Settings.model_validate(
        {
            "stage": "dev",
            "general": {
                "region": "ap-northeast-1",
                "project_name": "testprj",
                "stages": ["dev"],
            },
            "rds": {
                "managed": False,
                "secret_arn": "arn:aws:secretsmanager:ap-northeast-1:123:secret:my-db",
                "security_group_id": "sg-0123456789abcdef0",
            },
            "awscontainer": {
                "dockerfile_path": "Dockerfile",
            },
        }
    )
    assert settings.rds is not None
    assert not settings.rds.managed
    assert settings.rds.secret_arn is not None
    assert settings.rds.security_group_id is not None
    context = Context.from_settings(settings)
    assert context.rds is not None
    assert not context.rds.managed
    assert context.rds.secret_arn == settings.rds.secret_arn
    assert context.rds.security_group_id == settings.rds.security_group_id
    assert context.rds.vpc is None


def test_rds_unmanaged_requires_managed_false():
    """secret_arn 指定時に managed = false がないとエラー"""
    with pytest.raises(ValueError, match="managed = false"):
        Settings.model_validate(
            {
                "stage": "dev",
                "general": {
                    "region": "ap-northeast-1",
                    "project_name": "testprj",
                    "stages": ["dev"],
                },
                "rds": {
                    "secret_arn": "arn:aws:secretsmanager:ap-northeast-1:123:secret:x",
                    "security_group_id": "sg-xxx",
                },
                "awscontainer": {"dockerfile_path": "Dockerfile"},
            }
        )


def test_rds_unmanaged_rejects_managed_fields():
    """managed = false で作成系オプションはエラー"""
    with pytest.raises(ValueError, match="managed = false では"):
        Settings.model_validate(
            {
                "stage": "dev",
                "general": {
                    "region": "ap-northeast-1",
                    "project_name": "testprj",
                    "stages": ["dev"],
                },
                "rds": {
                    "managed": False,
                    "secret_arn": "arn:aws:secretsmanager:ap-northeast-1:123:secret:x",
                    "security_group_id": "sg-xxx",
                    "min_capacity": 1.0,
                },
                "awscontainer": {"dockerfile_path": "Dockerfile"},
            }
        )


def test_rds_unmanaged_requires_secret_arn():
    """managed = false で secret_arn 必須"""
    with pytest.raises(ValueError, match="secret_arn は必須"):
        Settings.model_validate(
            {
                "stage": "dev",
                "general": {
                    "region": "ap-northeast-1",
                    "project_name": "testprj",
                    "stages": ["dev"],
                },
                "rds": {
                    "managed": False,
                    "security_group_id": "sg-xxx",
                },
                "awscontainer": {"dockerfile_path": "Dockerfile"},
            }
        )


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


# --- password_strategy = "static" ---


def test_rds_static_password_strategy_managed_ok():
    """password_strategy=static は managed=true で受理される"""
    rds = RdsSettings.model_validate({"password_strategy": "static"})
    assert rds.managed is True
    assert rds.password_strategy == "static"


def test_rds_static_password_strategy_requires_managed():
    """password_strategy は managed=false では使用できない"""
    with pytest.raises(ValueError, match="password_strategy は managed = true"):
        RdsSettings.model_validate(
            {
                "managed": False,
                "secret_arn": "arn:aws:secretsmanager:ap-northeast-1:1:secret:x",
                "security_group_id": "sg-123",
                "password_strategy": "static",
            }
        )


def test_rds_context_static_secret_name(use_toml):
    """static でも credentials_secret_name が決まる (既定 aws-managed でも常設)"""
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.rds is not None
    assert (
        context.rds.credentials_secret_name == "dev-testprj-pocket-aurora-credentials"
    )


@mock_aws
def test_rds_static_password_creates_pocket_secret(use_toml):
    """static: pocket 所有の secret に password+host が保存され DATABASE_URL を組める"""
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.rds is not None
    context.rds.password_strategy = "static"

    from pocket_cli.resources.vpc import Vpc

    vpc = Vpc(context.rds.vpc)
    vpc.create()

    rds = Rds(context.rds)
    rds.create()

    # AWS マネージドの MasterUserSecret ではなく pocket 所有 secret を指す
    assert rds.master_user_secret_arn is not None
    assert rds.master_user_secret_kms_key_id is None
    assert "MasterUserSecret" not in (rds.cluster or {})

    sm = boto3.client("secretsmanager", region_name=context.rds.region)
    secret = sm.get_secret_value(SecretId=context.rds.credentials_secret_name)
    data = json.loads(secret["SecretString"])
    assert data["username"] == context.rds.master_username
    assert data["password"]
    assert data["host"]  # static は host も secret に含む
    assert data["dbname"] == context.rds.database_name

    os.environ["POCKET_RDS_SECRET_ARN"] = rds.master_user_secret_arn
    try:
        from pocket.runtime import _set_rds_database_url

        _set_rds_database_url()
        database_url = os.environ.get("DATABASE_URL", "")
        assert database_url.startswith("postgres://")
        assert data["host"] in database_url
        assert data["dbname"] in database_url
    finally:
        os.environ.pop("POCKET_RDS_SECRET_ARN", None)
        os.environ.pop("DATABASE_URL", None)


@mock_aws
def test_rds_static_password_deletes_pocket_secret(use_toml):
    """static: delete() で pocket 所有の secret も削除される"""
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.rds is not None
    context.rds.password_strategy = "static"

    from pocket_cli.resources.vpc import Vpc

    vpc = Vpc(context.rds.vpc)
    vpc.create()

    rds = Rds(context.rds)
    rds.create()
    assert rds.master_user_secret_arn is not None

    rds.delete()

    sm = boto3.client("secretsmanager", region_name=context.rds.region)
    with pytest.raises(sm.exceptions.ResourceNotFoundException):
        sm.describe_secret(SecretId=context.rds.credentials_secret_name)


def test_rds_static_secret_store_defaults_sm(use_toml):
    """awscontainer.secrets 未設定なら static の保存先は sm"""
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.rds is not None
    assert context.rds.secret_store == "sm"


def test_rds_static_secret_store_follows_toggle():
    """awscontainer.secrets.store=ssm のとき static の保存先も ssm になる"""
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
                "password_strategy": "static",
            },
            "awscontainer": {
                "dockerfile_path": "Dockerfile",
                "vpc": {"ref": "main", "zone_suffixes": ["a", "c"]},
                "secrets": {"store": "ssm"},
            },
        }
    )
    context = Context.from_settings(settings)
    assert context.rds is not None
    assert context.rds.secret_store == "ssm"
    assert context.rds.password_strategy == "static"


@mock_aws
def test_rds_static_ssm_store_creates_parameter_not_secret(use_toml):
    """static + store=ssm: SSM パラメータに保存し Secrets Manager には作らない"""
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.rds is not None
    context.rds.password_strategy = "static"
    context.rds.secret_store = "ssm"

    from pocket_cli.resources.vpc import Vpc

    vpc = Vpc(context.rds.vpc)
    vpc.create()

    rds = Rds(context.rds)
    rds.create()

    # Secrets Manager には secret を作らない
    assert rds.master_user_secret_arn is None
    assert rds.static_ssm_param_name == context.rds.credentials_secret_name

    ssm = boto3.client("ssm", region_name=context.rds.region)
    param = ssm.get_parameter(
        Name=context.rds.credentials_secret_name, WithDecryption=True
    )
    assert param["Parameter"]["Type"] == "SecureString"
    data = json.loads(param["Parameter"]["Value"])
    assert data["password"]
    assert data["host"]
    assert data["dbname"] == context.rds.database_name

    sm = boto3.client("secretsmanager", region_name=context.rds.region)
    with pytest.raises(sm.exceptions.ResourceNotFoundException):
        sm.describe_secret(SecretId=context.rds.credentials_secret_name)

    os.environ["POCKET_RDS_SECRET_STORE"] = "ssm"
    os.environ["POCKET_RDS_SSM_PARAM"] = context.rds.credentials_secret_name
    try:
        from pocket.runtime import _set_rds_database_url

        _set_rds_database_url()
        database_url = os.environ.get("DATABASE_URL", "")
        assert database_url.startswith("postgres://")
        assert data["host"] in database_url
        assert data["dbname"] in database_url
    finally:
        os.environ.pop("POCKET_RDS_SECRET_STORE", None)
        os.environ.pop("POCKET_RDS_SSM_PARAM", None)
        os.environ.pop("DATABASE_URL", None)


@mock_aws
def test_rds_static_ssm_store_deletes_parameter(use_toml):
    """static + store=ssm: delete() で SSM パラメータも削除される"""
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.rds is not None
    context.rds.password_strategy = "static"
    context.rds.secret_store = "ssm"

    from pocket_cli.resources.vpc import Vpc

    vpc = Vpc(context.rds.vpc)
    vpc.create()

    rds = Rds(context.rds)
    rds.create()
    rds.delete()

    ssm = boto3.client("ssm", region_name=context.rds.region)
    with pytest.raises(ssm.exceptions.ParameterNotFound):
        ssm.get_parameter(Name=context.rds.credentials_secret_name)


# --- password_strategy / store の移行 (pocket deploy で自動修正) ---


def _create_vpc_and_cluster(context):
    from pocket_cli.resources.vpc import Vpc

    Vpc(context.rds.vpc).create()
    rds = Rds(context.rds)
    rds.create()
    return rds


@mock_aws
def test_rds_migration_status_detects_drift(use_toml):
    """aws-managed で作成後 config を static にすると status が REQUIRE_UPDATE"""
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.rds is not None

    _create_vpc_and_cluster(context)  # 既定 aws-managed で作成
    assert Rds(context.rds).status == "COMPLETED"

    context.rds.password_strategy = "static"
    context.rds.secret_store = "sm"
    assert Rds(context.rds).status == "REQUIRE_UPDATE"


@mock_aws
def test_rds_migration_managed_to_static_sm(use_toml):
    """aws-managed → static(sm): update() でクラスタの managed を解除し secret を作成"""
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.rds is not None

    _create_vpc_and_cluster(context)  # aws-managed
    assert "MasterUserSecret" in (Rds(context.rds).cluster or {})

    context.rds.password_strategy = "static"
    context.rds.secret_store = "sm"
    Rds(context.rds).update()

    after = Rds(context.rds)
    assert after.status == "COMPLETED"
    # クラスタは managed 解除済み、pocket 所有 secret を参照
    assert "MasterUserSecret" not in (after.cluster or {})
    assert after.master_user_secret_arn is not None

    sm = boto3.client("secretsmanager", region_name=context.rds.region)
    data = json.loads(
        sm.get_secret_value(SecretId=context.rds.credentials_secret_name)[
            "SecretString"
        ]
    )
    assert data["password"]


@mock_aws
def test_rds_migration_static_to_managed(use_toml):
    """static(sm) → aws-managed: update() で managed に戻し pocket secret を削除"""
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.rds is not None
    context.rds.password_strategy = "static"
    context.rds.secret_store = "sm"

    _create_vpc_and_cluster(context)  # static(sm)
    assert Rds(context.rds).master_user_secret_arn is not None

    context.rds.password_strategy = "aws-managed"
    Rds(context.rds).update()

    after = Rds(context.rds)
    assert after.status == "COMPLETED"
    assert "MasterUserSecret" in (after.cluster or {})

    # pocket 所有 secret は削除されている
    sm = boto3.client("secretsmanager", region_name=context.rds.region)
    with pytest.raises(sm.exceptions.ResourceNotFoundException):
        sm.describe_secret(SecretId=context.rds.credentials_secret_name)


@mock_aws
def test_rds_migration_sm_to_ssm_preserves_password(use_toml):
    """static の sm → ssm 切替: パスワードを変えず credential を移送する"""
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.rds is not None
    context.rds.password_strategy = "static"
    context.rds.secret_store = "sm"

    _create_vpc_and_cluster(context)  # static(sm)
    sm = boto3.client("secretsmanager", region_name=context.rds.region)
    before = json.loads(
        sm.get_secret_value(SecretId=context.rds.credentials_secret_name)[
            "SecretString"
        ]
    )

    context.rds.secret_store = "ssm"
    assert Rds(context.rds).status == "REQUIRE_UPDATE"
    Rds(context.rds).update()

    after = Rds(context.rds)
    assert after.status == "COMPLETED"

    # SSM にパスワード変更なしで移送、SM 側は削除
    ssm = boto3.client("ssm", region_name=context.rds.region)
    moved = json.loads(
        ssm.get_parameter(
            Name=context.rds.credentials_secret_name, WithDecryption=True
        )["Parameter"]["Value"]
    )
    assert moved["password"] == before["password"]
    with pytest.raises(sm.exceptions.ResourceNotFoundException):
        sm.describe_secret(SecretId=context.rds.credentials_secret_name)
