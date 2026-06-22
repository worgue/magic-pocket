"""user secret の stored mode (type = neon_database_url / tidb_database_url) のテスト。

stored mode は「事前 provision 済みの接続 URL を参照するだけ」で、pocket は provider の
管理 API を一切叩かない。検証対象:

- name / type の排他バリデーション
- type 指定時の正準名導出 (sm / ssm)、managed pocket_store パスと衝突しないこと
- 導出名への IAM (GetSecretValue / GetParameter) 自動付与
- deploy 時の存在チェック (未 provision なら正準名つきで raise)
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError
from pocket_cli.mediator import Mediator
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
    """type 指定 (store=sm) → {pocket_key}-user/{ENV_KEY} を導出する。"""
    secrets = settings.Secrets(
        store="sm",
        user={"DATABASE_URL": settings.UserSecretSpec(type="tidb_database_url")},
    )
    ctx = SecretsContext.from_settings(secrets, base_settings)
    # pocket_key = test-testprj-pocket
    assert ctx.user["DATABASE_URL"].name == "test-testprj-pocket-user/DATABASE_URL"


def test_stored_type_name_derivation_ssm(base_settings):
    """type 指定 (store=ssm) → /{pocket_key}-user/{ENV_KEY} を導出する。"""
    secrets = settings.Secrets(
        store="ssm",
        user={"DATABASE_URL": settings.UserSecretSpec(type="neon_database_url")},
    )
    ctx = SecretsContext.from_settings(secrets, base_settings)
    assert ctx.user["DATABASE_URL"].name == "/test-testprj-pocket-user/DATABASE_URL"


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
        "test-testprj-pocket-user/DATABASE_URL" in ctx.allowed_sm_resources
    )


def test_stored_type_iam_ssm(base_settings):
    secrets = settings.Secrets(
        store="ssm",
        user={"DATABASE_URL": settings.UserSecretSpec(type="neon_database_url")},
    )
    ctx = SecretsContext.from_settings(secrets, base_settings)
    assert (
        "arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter"
        "/test-testprj-pocket-user/DATABASE_URL" in ctx.allowed_ssm_resources
    )


# --- deploy 時の存在チェック --------------------------------------------------


def _ctx_stub(secrets_context) -> Any:
    """Mediator が触る self.context.awscontainer.secrets だけを持つ stub。"""

    class _Aws:
        secrets = secrets_context

    class _Ctx:
        awscontainer = _Aws()

    return _Ctx()


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
    mediator = Mediator(_ctx_stub(sc))
    # 存在すれば例外を投げない
    mediator.verify_user_stored_secrets()


def test_verify_stored_secret_missing_ssm_raises(base_settings, patch_boto3):
    patch_boto3(missing=True)
    sc = _make_secrets_context(base_settings, "ssm", "neon_database_url")
    mediator = Mediator(_ctx_stub(sc))
    with pytest.raises(RuntimeError) as ei:
        mediator.verify_user_stored_secrets()
    # 正準名がエラーに含まれること (利用者が provision 先を特定できる)
    assert "/test-testprj-pocket-user/DATABASE_URL" in str(ei.value)


def test_verify_stored_secret_missing_sm_raises(base_settings, patch_boto3):
    patch_boto3(missing=True)
    sc = _make_secrets_context(base_settings, "sm", "tidb_database_url")
    mediator = Mediator(_ctx_stub(sc))
    with pytest.raises(RuntimeError) as ei:
        mediator.verify_user_stored_secrets()
    assert "test-testprj-pocket-user/DATABASE_URL" in str(ei.value)


def test_verify_ignores_non_typed_user_secret(base_settings, patch_boto3):
    """type の無い (name 指定の) 従来 user secret は存在チェック対象外。"""
    patch_boto3(missing=True)
    secrets = settings.Secrets(
        store="ssm",
        user={"TOKEN": settings.UserSecretSpec(name="/svc/token")},
    )
    sc = SecretsContext.from_settings(secrets, base_settings)
    mediator = Mediator(_ctx_stub(sc))
    # name のみの user secret は missing でも raise しない
    mediator.verify_user_stored_secrets()
