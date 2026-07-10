"""pocket.naming の公開 API (stored user secret 名の正準導出) のテスト。

外部 provisioner が import して依存する契約なので、導出結果を固定する。
"""

from __future__ import annotations

import pocket
from pocket.context import user_secret_path as context_user_secret_path
from pocket.naming import (
    TIDB_DATABASE_URL,
    pocket_key,
    stored_user_secret_name,
    user_secret_path,
)


def test_pocket_key_default_format():
    assert (
        pocket_key(project="pocket-example", stage="sandbox")
        == "sandbox-pocket-example-pocket"
    )


def test_stored_user_secret_name_ssm():
    """deploy が type= で読むパスと一致する (SSM)。"""
    assert (
        stored_user_secret_name(
            project="pocket-example", stage="sandbox", secret_type=TIDB_DATABASE_URL
        )
        == "/sandbox-pocket-example-pocket-user/tidb_database_url"
    )


def test_stored_user_secret_name_sm_has_no_leading_slash():
    assert (
        stored_user_secret_name(
            project="p", stage="prod", secret_type=TIDB_DATABASE_URL, store="sm"
        )
        == "prod-p-pocket-user/tidb_database_url"
    )


def test_custom_namespace_and_format():
    assert (
        stored_user_secret_name(
            project="p",
            stage="dev",
            secret_type=TIDB_DATABASE_URL,
            namespace="ns",
            pocket_key_format="{project}-{stage}",
        )
        == "/p-dev-user/tidb_database_url"
    )


def test_backward_compat_context_reexport():
    """`from pocket.context import user_secret_path` は同一関数を指す (後方互換)。"""
    assert context_user_secret_path is user_secret_path
    name = context_user_secret_path(
        "sandbox-pocket-example-pocket", "neon_database_url", "ssm"
    )
    assert name == "/sandbox-pocket-example-pocket-user/neon_database_url"


def test_exposed_at_package_root():
    assert pocket.stored_user_secret_name is stored_user_secret_name
    assert pocket.TIDB_DATABASE_URL == "tidb_database_url"
