from __future__ import annotations

from django.db.backends.postgresql.base import (
    DatabaseWrapper as PostgresqlDatabaseWrapper,
)

from pocket.django.db_backends.rds.credentials import connect_with_credential_refresh


class DatabaseWrapper(PostgresqlDatabaseWrapper):
    """RDS 用 PostgreSQL backend。

    master password の自動ローテーション (``ManageMasterUserPassword=True``) 直後に
    warm Lambda が古いパスワードで再接続して認証失敗する問題に対処する。接続確立時に
    認証エラーを捕捉したら RDS シークレットを再取得して 1 度だけ再接続し、cold start を
    待たずに自己修復する。
    """

    def get_new_connection(self, conn_params):
        return connect_with_credential_refresh(
            super().get_new_connection,
            conn_params,
            self.settings_dict,
            self.get_connection_params,
        )
