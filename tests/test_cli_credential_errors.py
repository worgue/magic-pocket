"""CLI 層の認証系例外ハンドリングのテスト。

SSO 失効状態で `pocket resource dsql endpoint` 等を実行すると botocore の
traceback が約 100 行流れ、「aws sso login し直せばよい」という結論に
辿り着けなかった実害への回帰テスト (2026-08-25 起票の feedback KN1255)。
CLI 層 (PocketCLI.invoke) が捕捉して認証ガイド + 非ゼロ exit に変換する。
"""

import logging

import click
import pytest
from botocore.exceptions import (
    NoCredentialsError,
    ProfileNotFound,
    SSOTokenLoadError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)
from click.testing import CliRunner
from pocket_cli.cli.main_cli import PocketCLI, main


def _cli_raising(exc: BaseException) -> click.Group:
    @click.group(cls=PocketCLI)
    def cli():
        pass

    @cli.command()
    def boom():
        raise exc

    return cli


@pytest.mark.parametrize(
    "exc,fragment",
    [
        (
            TokenRetrievalError(
                provider="sso", error_msg="Token has expired and refresh failed"
            ),
            "SSO トークンの有効期限が切れています",
        ),
        (UnauthorizedSSOTokenError(), "SSO トークンの有効期限が切れています"),
        (
            SSOTokenLoadError(error_msg="x"),
            "SSO セッションが見つかりません",
        ),
        (NoCredentialsError(), "AWS 認証情報が見つかりません"),
        (ProfileNotFound(profile="nope"), "nope"),
    ],
)
def test_credential_error_becomes_guide_without_traceback(exc, fragment):
    result = CliRunner().invoke(_cli_raising(exc), ["boom"])
    assert result.exit_code == 1
    # botocore 例外のまま伝播していないこと (SystemExit = ctx.exit へ変換済み)
    assert result.exception is None or isinstance(result.exception, SystemExit)
    err = result.stderr.replace("\n", "")
    assert fragment in err
    assert "aws sso login" in err
    assert "Traceback" not in err


def test_non_credential_exceptions_still_propagate():
    result = CliRunner().invoke(_cli_raising(RuntimeError("real bug")), ["boom"])
    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)


def test_main_suppresses_botocore_tokens_warning_logs():
    """botocore.tokens の WARNING (exc_info 付き traceback) を抑制する"""
    logger = logging.getLogger("botocore.tokens")
    logger.setLevel(logging.NOTSET)
    CliRunner().invoke(main, ["version"])
    assert not logger.isEnabledFor(logging.WARNING)
