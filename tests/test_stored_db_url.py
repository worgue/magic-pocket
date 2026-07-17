"""user secret の stored mode (type = neon_database_url / tidb_database_url) のテスト。

stored mode は「事前 provision 済みの接続 URL を参照するだけ」で、pocket は provider の
管理 API を一切叩かない。検証対象:

- name / type の排他バリデーション
- type 指定時の正準名導出 (sm / ssm)、managed pocket_store パスと衝突しないこと
- 導出名への IAM (GetSecretValue / GetParameter) 自動付与
- deploy 時の存在チェック (未 provision なら正準名つきで raise)
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError

from pocket import settings
from pocket.context import SecretsContext

# --- バリデーション (name / type 排他) ---------------------------------------


def test_user_secret_type_alone_is_valid():
    spec = settings.UserSecretSpec(type="tidb_database_url")
    assert spec.type == "tidb_database_url"
    assert spec.name is None


def test_user_secret_name_alone_is_valid():
    spec = settings.UserSecretSpec(name="/svc/db-url", store="ssm")
    assert spec.name == "/svc/db-url"
    assert spec.type is None


def test_user_secret_both_name_and_type_is_error():
    # 「両方指定」はユーザー入力時点 (Secrets) で弾く
    with pytest.raises(ValidationError):
        settings.Secrets(
            user={
                "DATABASE_URL": settings.UserSecretSpec(
                    name="/svc/db-url", type="tidb_database_url"
                )
            }
        )


def test_user_secret_neither_name_nor_type_is_error():
    with pytest.raises(ValidationError):
        settings.UserSecretSpec(store="sm")


# --- 正準名の導出 -------------------------------------------------------------


def test_stored_type_name_derivation_sm(base_settings):
    """type 指定 (store=sm) → {pocket_key}-user/{type} を導出する (env 名に非依存)。"""
    secrets = settings.Secrets(
        store="sm",
        user={"DATABASE_URL": settings.UserSecretSpec(type="tidb_database_url")},
    )
    ctx = SecretsContext.from_settings(secrets, base_settings)
    # pocket_key = test-testprj-pocket / 保存 identity は type 基準 (辞書キー非依存)
    assert ctx.user["DATABASE_URL"].name == "test-testprj-pocket-user/tidb_database_url"


def test_stored_type_name_derivation_ssm(base_settings):
    """type 指定 (store=ssm) → /{pocket_key}-user/{type} を導出する。"""
    secrets = settings.Secrets(
        store="ssm",
        user={"DATABASE_URL": settings.UserSecretSpec(type="neon_database_url")},
    )
    ctx = SecretsContext.from_settings(secrets, base_settings)
    assert (
        ctx.user["DATABASE_URL"].name == "/test-testprj-pocket-user/neon_database_url"
    )


def test_stored_type_name_independent_of_env_key(base_settings):
    """env var 名 (辞書キー) を変えても保存パスは type 基準で不変。"""
    a = SecretsContext.from_settings(
        settings.Secrets(
            store="ssm",
            user={"DATABASE_URL": settings.UserSecretSpec(type="neon_database_url")},
        ),
        base_settings,
    )
    b = SecretsContext.from_settings(
        settings.Secrets(
            store="ssm",
            user={"NEON_URL": settings.UserSecretSpec(type="neon_database_url")},
        ),
        base_settings,
    )
    assert a.user["DATABASE_URL"].name == b.user["NEON_URL"].name


def test_user_secret_duplicate_type_is_error():
    """同一 type の user secret 複数宣言は保存パス衝突のため禁止。"""
    with pytest.raises(ValidationError):
        settings.Secrets(
            store="ssm",
            user={
                "DB1": settings.UserSecretSpec(type="neon_database_url"),
                "DB2": settings.UserSecretSpec(type="neon_database_url"),
            },
        )


def test_stored_type_name_does_not_collide_with_managed_path(base_settings):
    """導出名は managed の pocket_store パス (/{pocket_key}/...) の配下に入らない。

    cleanup (_cleanup_orphaned_secrets) は /{pocket_key}/ 配下のみ走査するため、
    -user prefix により stored secret が誤って削除されないことを担保する。
    """
    secrets = settings.Secrets(
        store="ssm",
        user={"DATABASE_URL": settings.UserSecretSpec(type="tidb_database_url")},
    )
    ctx = SecretsContext.from_settings(secrets, base_settings)
    name = ctx.user["DATABASE_URL"].name
    assert name is not None
    assert not name.startswith("/test-testprj-pocket/")
    assert name.startswith("/test-testprj-pocket-user/")


# --- IAM 付与 -----------------------------------------------------------------


def test_stored_type_iam_sm(base_settings):
    secrets = settings.Secrets(
        store="sm",
        user={"DATABASE_URL": settings.UserSecretSpec(type="tidb_database_url")},
    )
    ctx = SecretsContext.from_settings(secrets, base_settings)
    assert (
        "arn:aws:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:"
        # SM の実 ARN のランダムサフィックスにマッチするワイルドカード付き
        "test-testprj-pocket-user/tidb_database_url-??????" in ctx.allowed_sm_resources
    )


def test_stored_type_iam_ssm(base_settings):
    secrets = settings.Secrets(
        store="ssm",
        user={"DATABASE_URL": settings.UserSecretSpec(type="neon_database_url")},
    )
    ctx = SecretsContext.from_settings(secrets, base_settings)
    assert (
        "arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter"
        "/test-testprj-pocket-user/neon_database_url" in ctx.allowed_ssm_resources
    )


def test_user_secret_sm_bare_name_gets_suffix_wildcard(base_settings):
    """SM の名前指定 user secret に -?????? ワイルドカードが付くこと

    SM の実 ARN は名前末尾に 6 文字のランダムサフィックスが付くため、
    exact 名の Resource では実 secret にマッチせず AccessDenied になる。
    """
    secrets = settings.Secrets(
        store="sm",
        user={"API_KEY": settings.UserSecretSpec(name="my-api-key")},
    )
    ctx = SecretsContext.from_settings(secrets, base_settings)
    assert (
        "arn:aws:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:"
        "my-api-key-??????" in ctx.allowed_sm_resources
    )


def test_user_secret_sm_full_arn_untouched(base_settings):
    """ARN 指定はそのまま (ワイルドカードを付けない) こと"""
    arn = "arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:x-AbCdEf"
    secrets = settings.Secrets(
        store="sm",
        user={"API_KEY": settings.UserSecretSpec(name=arn)},
    )
    ctx = SecretsContext.from_settings(secrets, base_settings)
    assert arn in ctx.allowed_sm_resources


def test_user_secret_ssm_bare_name_normalized(base_settings):
    """先頭 / なしの SSM パラメータ名から正しい ARN が生成されること"""
    secrets = settings.Secrets(
        store="ssm",
        user={"API_KEY": settings.UserSecretSpec(name="myparam", store="ssm")},
    )
    ctx = SecretsContext.from_settings(secrets, base_settings)
    assert (
        "arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/myparam"
        in ctx.allowed_ssm_resources
    )


# --- deploy 時の存在チェック --------------------------------------------------


def _make_secrets_context(base_settings, store, type_):
    secrets = settings.Secrets(
        store=store,
        user={"DATABASE_URL": settings.UserSecretSpec(type=type_)},
    )
    return SecretsContext.from_settings(secrets, base_settings)


class _FakeClient:
    def __init__(self, missing: bool):
        self._missing = missing

    def get_parameter(self, Name):  # noqa: N803 (boto3 API 名)
        if self._missing:
            raise ClientError({"Error": {"Code": "ParameterNotFound"}}, "GetParameter")
        return {"Parameter": {"Value": "mysql://app:pw@host:4000/db"}}

    def describe_secret(self, SecretId):  # noqa: N803
        if self._missing:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException"}}, "DescribeSecret"
            )
        return {"ARN": "arn:aws:secretsmanager:...:secret:x"}


@pytest.fixture
def patch_boto3(monkeypatch):
    def _patch(missing: bool):
        monkeypatch.setattr(
            "boto3.client", lambda *a, **k: _FakeClient(missing=missing)
        )

    return _patch


def test_verify_stored_secret_present_ssm(base_settings, patch_boto3):
    patch_boto3(missing=False)
    sc = _make_secrets_context(base_settings, "ssm", "neon_database_url")
    # 存在すれば例外を投げない
    sc.user_store.verify_provisioned()


def test_verify_stored_secret_missing_ssm_raises(base_settings, patch_boto3):
    patch_boto3(missing=True)
    sc = _make_secrets_context(base_settings, "ssm", "neon_database_url")
    with pytest.raises(RuntimeError) as ei:
        sc.user_store.verify_provisioned()
    # 正準名がエラーに含まれること (利用者が provision 先を特定できる)
    assert "/test-testprj-pocket-user/neon_database_url" in str(ei.value)


def test_verify_stored_secret_missing_sm_raises(base_settings, patch_boto3):
    patch_boto3(missing=True)
    sc = _make_secrets_context(base_settings, "sm", "tidb_database_url")
    with pytest.raises(RuntimeError) as ei:
        sc.user_store.verify_provisioned()
    assert "test-testprj-pocket-user/tidb_database_url" in str(ei.value)


def test_verify_ignores_non_typed_user_secret(base_settings, patch_boto3):
    """type の無い (name 指定の) 従来 user secret は存在チェック対象外。"""
    patch_boto3(missing=True)
    secrets = settings.Secrets(
        store="ssm",
        user={"TOKEN": settings.UserSecretSpec(name="/svc/token")},
    )
    sc = SecretsContext.from_settings(secrets, base_settings)
    # name のみの user secret は missing でも raise しない
    sc.user_store.verify_provisioned()
