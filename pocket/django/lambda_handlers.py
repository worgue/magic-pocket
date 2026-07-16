import json
import os
from subprocess import run

from apig_wsgi import make_lambda_handler
from django.core.management import call_command

from pocket.django.utils import pocket_delete_sqs_task

from ..utils import MANAGE_HANDLER_SUCCESS_SENTINEL, get_wsgi_application

wsgi_handler = make_lambda_handler(
    get_wsgi_application(),
    binary_support=True,
    non_binary_content_type_prefixes=(
        "application/json",
        "application/vnd.api+json",
    ),
)


def _handle_resetdb():
    """public スキーマを DROP して再作成する"""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public")
    print("resetdb: public スキーマをリセットしました")


def management_command_handler(event, context):
    print(event)
    # EventBridge Scheduler 経由 (pocket.django.management_lambda_scheduler) からは
    # shell-style 文字列 1 本が "manage" キーで渡る
    if "manage" in event:
        import shlex

        tokens = shlex.split(event["manage"])
        if not tokens:
            raise ValueError("manage event must contain a non-empty command line")
        call_command(*tokens)
    else:
        command = event["command"]
        args = event.get("args") or []
        kwargs = event.get("kwargs") or {}
        print(command)
        print("args:", args)
        print("kwargs:", kwargs)
        if command == "pocket_resetdb":
            _handle_resetdb()
        else:
            if command == "createsuperuser" and not os.environ.get(
                "DJANGO_SUPERUSER_PASSWORD"
            ):
                raise Exception("DJANGO_SUPERUSER_PASSWORD is not set")
            call_command(command, *args, **kwargs)
    # ここに到達 = 例外なく完了。非同期 invoke でも CLI が成否を判定できるよう、
    # 成功時だけセンチネルを印字する (失敗時は例外が伝播しここには来ない)。
    print(MANAGE_HANDLER_SUCCESS_SENTINEL)


def _run_sqs_management_command_record(record):
    """SQS record 1 件の management command を実行し、成功した message を削除する。"""
    print(record["body"])
    data = json.loads(record["body"])
    call_command(data["command"], *data["args"], **data["kwargs"])
    pocket_delete_sqs_task(record["receiptHandle"])


def sqs_management_command_handler(event, context):
    print(event)
    for record in event["Records"]:
        _run_sqs_management_command_record(record)


def sqs_management_command_report_failures_handler(event, context):
    print(event)
    batch_item_failures = []
    for record in event["Records"]:
        try:
            _run_sqs_management_command_record(record)
        # batchItemFailures で失敗 record を SQS に報告するには、management
        # command が投げる任意の例外を捕捉する必要がある (仕組み上の要請)
        except Exception as e:
            print(e)
            batch_item_failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": batch_item_failures}


def dangerous_shell_handler(event, context):
    """event["command_line"] を ``shell=True`` でそのまま実行する危険な handler.

    **任意の文字列を shell で実行できる**ため、event source に untrusted な入力が
    (直接・間接問わず) 到達すると即 RCE になる。output capture も job-state 連携も
    無い。本当に任意 shell が必要な、信頼できる呼び出し元 (= 既に同等の権限を持つ
    オペレータが手動 invoke する等) でのみ使うこと。

    SQS 駆動でコマンドを安全に完走させたい用途には :class:`pocket.command_handler.
    BaseCommandHandler` を使う (実行ファイル固定 + list argv + ``shell=False`` +
    出力 / ステータスの sink + crash 時 finalize)。
    """
    print(event)
    command_line = event["command_line"]
    run(command_line, shell=True, check=True)  # noqa: S602 docstring 参照: 信頼できる呼び出し元限定の危険 handler  # nosemgrep
