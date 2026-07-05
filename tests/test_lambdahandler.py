"""LambdaHandler.show_logs の成否判定 (management command の false green 防止)。

management_command_handler は非同期 (InvocationType="Event") で invoke されるため、
ハンドラの例外は呼び出し側に伝わらない。show_logs は成功センチネルが REPORT 行
までに現れたかどうかで成否を判定し、現れなければ ManagementCommandFailed を送出する。
"""

import pytest
from pocket_cli.resources.aws.lambdahandler import (
    LambdaHandler,
    ManagementCommandFailed,
)

from pocket.utils import MANAGE_HANDLER_SUCCESS_SENTINEL


class _FakeLogsClient:
    def __init__(self, messages):
        self._messages = messages

    def filter_log_events(self, **kwargs):
        return {"events": [{"message": m} for m in self._messages]}


def _make_handler(monkeypatch, messages):
    request_id = "test-request-id"
    handler = object.__new__(LambdaHandler)  # __init__ (boto client 生成) を回避
    fake_context = type("Ctx", (), {"log_group_name": "lg"})()
    # setattr 経由で入れて pyright の型チェック (LambdaHandlerContext 期待) を回避
    monkeypatch.setattr(handler, "context", fake_context, raising=False)
    monkeypatch.setattr(
        handler, "logs_client", _FakeLogsClient(messages), raising=False
    )
    monkeypatch.setattr(
        handler,
        "_find_events",
        lambda *a, **k: [{"logStreamName": "s", "timestamp": 0}],
        raising=False,
    )
    invoke_response = {
        "ResponseMetadata": {
            "RequestId": request_id,
            "HTTPHeaders": {"date": "Wed, 21 Oct 2026 07:28:00 GMT"},
        }
    }
    return handler, invoke_response, request_id


def test_show_logs_returns_on_success_sentinel(monkeypatch):
    """成功センチネルが REPORT 前に出ていれば正常終了する。"""
    _, _, request_id = _make_handler(monkeypatch, [])
    messages = [
        '"START RequestId: %s"' % request_id,
        "Running migrations...",
        MANAGE_HANDLER_SUCCESS_SENTINEL,
        "REPORT RequestId: %s\tDuration: 1 ms" % request_id,
    ]
    handler, invoke_response, _ = _make_handler(monkeypatch, messages)
    # 例外を出さずに返れば成功
    handler.show_logs(invoke_response, timeout_seconds=5)


def test_show_logs_raises_without_success_sentinel(monkeypatch):
    """センチネルが無いまま REPORT に達したら失敗として送出する (false green 防止)。"""
    _, _, request_id = _make_handler(monkeypatch, [])
    messages = [
        '"START RequestId: %s"' % request_id,
        "Running migrations...",
        "[ERROR] django.db.utils.ProgrammingError: relation does not exist",
        "REPORT RequestId: %s\tDuration: 1 ms" % request_id,
    ]
    handler, invoke_response, _ = _make_handler(monkeypatch, messages)
    with pytest.raises(ManagementCommandFailed) as exc:
        handler.show_logs(invoke_response, timeout_seconds=5)
    # アプリ例外由来なので従来どおり traceback 確認へ誘導する
    assert "traceback" in str(exc.value)


def test_show_logs_reframes_init_phase_failure(monkeypatch):
    """INIT フェーズ失敗 (Runtime.Unknown) は版不整合を疑うメッセージに切り替える。

    INIT で落ちると管理ハンドラは走らないため、「アプリの traceback を確認」という
    従来メッセージは誤誘導になる。runtime 側 (版不整合含む) を疑う案内に出し分ける。
    """
    _, _, request_id = _make_handler(monkeypatch, [])
    messages = [
        "INIT_REPORT Init Duration: 1836.44 ms\tPhase: invoke\t"
        "Status: error\tError Type: Runtime.Unknown",
        '"START RequestId: %s"' % request_id,
        "END RequestId: %s" % request_id,
        "REPORT RequestId: %s\tDuration: 1 ms" % request_id,
    ]
    handler, invoke_response, _ = _make_handler(monkeypatch, messages)
    with pytest.raises(ManagementCommandFailed) as exc:
        handler.show_logs(invoke_response, timeout_seconds=5)
    msg = str(exc.value)
    assert "INIT" in msg
    assert "magic-pocket" in msg  # version 不整合の対処 (uv add) に誘導
