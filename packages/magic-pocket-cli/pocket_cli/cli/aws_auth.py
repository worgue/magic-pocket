from __future__ import annotations

import boto3
from botocore.exceptions import (
    ClientError,
    NoCredentialsError,
    TokenRetrievalError,
)

from pocket.utils import echo


def check_aws_credentials() -> None:
    """AWS 認証情報が有効か確認し、無効ならガイドを表示して終了する"""
    try:
        sts = boto3.client("sts")
        sts.get_caller_identity()
    except TokenRetrievalError:
        _print_auth_guide("SSO トークンの有効期限が切れています。")
        raise SystemExit(1) from None
    except NoCredentialsError:
        _print_auth_guide("AWS 認証情報が見つかりません。")
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
