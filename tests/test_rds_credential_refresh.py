import boto3
import pytest
from moto import mock_aws

from pocket.django.db_backends.rds import credentials
from pocket.django.db_backends.rds.credentials import (
    connect_with_credential_refresh,
    is_auth_error,
    refresh_rds_settings,
)


class FakeDbError(Exception):
    """sqlstate を持つ psycopg ライクな例外 (psycopg 非依存でテストするため)。"""

    def __init__(self, msg="", sqlstate=None):
        super().__init__(msg)
        self.sqlstate = sqlstate


# --- is_auth_error ---


def test_is_auth_error_sqlstate_class_28():
    assert is_auth_error(FakeDbError("nope", sqlstate="28P01"))
    assert is_auth_error(FakeDbError("nope", sqlstate="28000"))


def test_is_auth_error_pgcode_attr():
    class E(Exception):
        pgcode = "28P01"

    assert is_auth_error(E())


def test_is_auth_error_message_only():
    assert is_auth_error(
        Exception('FATAL: password authentication failed for user "app"')
    )


def test_is_auth_error_wrapped_in_cause():
    inner = FakeDbError("x", sqlstate="28P01")
    outer = Exception("django wrapped")
    outer.__cause__ = inner
    assert is_auth_error(outer)


def test_is_auth_error_non_auth_returns_false():
    assert not is_auth_error(FakeDbError("too many connections", sqlstate="53300"))
    assert not is_auth_error(Exception("could not connect to server"))
    assert not is_auth_error(None)


def test_is_auth_error_cyclic_chain_terminates():
    a = Exception("a")
    b = Exception("b")
    a.__cause__ = b
    b.__cause__ = a
    # 無限ループせず False を返すこと
    assert is_auth_error(a) is False


# --- refresh_rds_settings ---


def test_refresh_rds_settings_no_secret_arn(monkeypatch):
    monkeypatch.delenv("POCKET_RDS_SECRET_ARN", raising=False)
    settings_dict = {"PASSWORD": "old"}
    assert refresh_rds_settings(settings_dict) is False
    assert settings_dict == {"PASSWORD": "old"}


@mock_aws
def test_refresh_rds_settings_updates_credentials(monkeypatch):
    sm = boto3.client("secretsmanager", region_name="ap-northeast-1")
    arn = sm.create_secret(
        Name="rds-master",
        SecretString='{"username": "pocket", "password": "n3w:p@ss/word"}',
    )["ARN"]
    monkeypatch.setenv("POCKET_RDS_SECRET_ARN", arn)
    monkeypatch.setenv("POCKET_RDS_ENDPOINT", "db.example.com")
    monkeypatch.setenv("POCKET_RDS_PORT", "5432")
    monkeypatch.setenv("POCKET_RDS_DBNAME", "appdb")
    # _set_rds_database_url が os.environ["DATABASE_URL"] を直接書くため、
    # teardown で確実に消えるよう monkeypatch 管理下に置く
    monkeypatch.setenv("DATABASE_URL", "")

    settings_dict = {
        "USER": "old",
        "PASSWORD": "old",
        "HOST": "old",
        "PORT": "1",
        "NAME": "old",
    }
    assert refresh_rds_settings(settings_dict) is True
    # secret の値で上書きされ、特殊文字は unquote されていること
    assert settings_dict["USER"] == "pocket"
    assert settings_dict["PASSWORD"] == "n3w:p@ss/word"
    assert settings_dict["HOST"] == "db.example.com"
    assert settings_dict["PORT"] == "5432"
    assert settings_dict["NAME"] == "appdb"


# --- connect_with_credential_refresh ---


def test_connect_success_does_not_refresh(monkeypatch):
    calls = []

    def connect(params):
        calls.append(params)
        return "conn"

    monkeypatch.setattr(
        credentials,
        "refresh_rds_settings",
        lambda sd: pytest.fail("refresh must not run on success"),
    )
    result = connect_with_credential_refresh(connect, {"p": 1}, {}, lambda: {"p": 2})
    assert result == "conn"
    assert calls == [{"p": 1}]


def test_connect_retries_once_on_auth_error(monkeypatch):
    attempts = []

    def connect(params):
        attempts.append(params)
        if len(attempts) == 1:
            raise FakeDbError("password authentication failed", sqlstate="28P01")
        return "conn-2"

    settings_dict = {"PASSWORD": "old"}

    def fake_refresh(sd):
        sd["PASSWORD"] = "new"
        return True

    monkeypatch.setattr(credentials, "refresh_rds_settings", fake_refresh)
    result = connect_with_credential_refresh(
        connect,
        {"password": "old"},
        settings_dict,
        lambda: {"password": settings_dict["PASSWORD"]},
    )
    assert result == "conn-2"
    assert len(attempts) == 2
    # 再接続は refresh 後の最新パラメータで行われること
    assert attempts[1] == {"password": "new"}
    assert settings_dict["PASSWORD"] == "new"


def test_connect_non_auth_error_propagates_without_refresh(monkeypatch):
    def connect(params):
        raise FakeDbError("disk full", sqlstate="53100")

    monkeypatch.setattr(
        credentials,
        "refresh_rds_settings",
        lambda sd: pytest.fail("refresh must not run for non-auth errors"),
    )
    with pytest.raises(FakeDbError):
        connect_with_credential_refresh(connect, {}, {}, lambda: {})


def test_connect_auth_error_reraises_when_no_rds_secret(monkeypatch):
    err = FakeDbError("password authentication failed", sqlstate="28P01")

    def connect(params):
        raise err

    # RDS secret が無い (refresh が False) 場合は元の認証エラーを再送出
    monkeypatch.setattr(credentials, "refresh_rds_settings", lambda sd: False)
    with pytest.raises(FakeDbError):
        connect_with_credential_refresh(connect, {}, {}, lambda: {})


# --- engine 選択 / backend ロード ---


def test_detect_engine_rds_uses_custom_backend(use_toml):
    use_toml("tests/data/toml/rds.toml")
    from pocket.django.utils import _detect_engine

    assert _detect_engine("dev", "postgres") == "pocket.django.db_backends.rds"


def test_detect_engine_neon_unaffected(use_toml):
    use_toml("tests/data/toml/default.toml")
    from pocket.django.utils import _detect_engine

    assert _detect_engine("dev", "postgres") == "django.db.backends.postgresql"


def test_rds_backend_loads_and_overrides_get_new_connection():
    pytest.importorskip("psycopg")
    import django
    from django.conf import settings as dj_settings

    if not dj_settings.configured:
        dj_settings.configure(DATABASES={}, INSTALLED_APPS=[])
        django.setup()

    from django.db.backends.postgresql.base import DatabaseWrapper as Pg
    from django.db.utils import load_backend

    mod = load_backend("pocket.django.db_backends.rds")
    assert issubclass(mod.DatabaseWrapper, Pg)
    assert mod.DatabaseWrapper.get_new_connection is not Pg.get_new_connection
    assert mod.DatabaseWrapper.vendor == "postgresql"
