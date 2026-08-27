from __future__ import annotations

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    ProfileNotFound,
    SSOTokenLoadError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)

from pocket.utils import echo

# 認証情報が無効なときに botocore が送出する例外。CLI 層 (PocketCLI.invoke) が
# これを捕捉して traceback でなく認証ガイドに変換する (どのサブコマンドでも
# boto3 呼び出しの途中で送出されうるため、precheck では網羅できない)。
CREDENTIAL_EXCEPTIONS = (
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
    SSOTokenLoadError,
    NoCredentialsError,
    ProfileNotFound,
)


def print_credential_guide(e: BotoCoreError) -> None:
    """認証系例外を 1 行の原因 + 認証手順の案内に変換して表示する"""
    if isinstance(e, (TokenRetrievalError, UnauthorizedSSOTokenError)):
        _print_auth_guide("SSO トークンの有効期限が切れています。")
    elif isinstance(e, SSOTokenLoadError):
        _print_auth_guide("SSO セッションが見つかりません (未ログイン)。")
    elif isinstance(e, ProfileNotFound):
        _print_auth_guide(str(e))
    else:
        _print_auth_guide("AWS 認証情報が見つかりません。")


def check_aws_credentials() -> None:
    """AWS 認証情報が有効か確認し、無効ならガイドを表示して終了する"""
    try:
        sts = boto3.client("sts")
        sts.get_caller_identity()
    except CREDENTIAL_EXCEPTIONS as e:
        print_credential_guide(e)
        raise SystemExit(1) from None
    except ClientError as e:
        if e.response["Error"]["Code"] in (
            "ExpiredToken",
            "ExpiredTokenException",
        ):
            _print_auth_guide("AWS 認証トークンの有効期限が切れています。")
            raise SystemExit(1) from None
        raise


def _print_auth_guide(message: str) -> None:
    echo.danger(message)
    echo.info("")
    echo.info("以下のいずれかで認証してください:")
    echo.info("")
    echo.info("  SSO の場合:")
    echo.info("    aws sso login")
    echo.info("    aws sso login --profile <profile-name>")
    echo.info("")
    echo.info("  IAM ユーザーの場合:")
    echo.info("    aws configure")
    echo.info("")
    echo.info("  環境変数の場合:")
    echo.info("    export AWS_ACCESS_KEY_ID=...")
    echo.info("    export AWS_SECRET_ACCESS_KEY=...")
