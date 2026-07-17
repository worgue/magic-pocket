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


def test_wait_update_raises_on_failed(monkeypatch):
    """LastUpdateStatus=Failed で wait_update が例外を投げること

    以前は PROGRESS 以外で一律 break して「was updated.」と表示し、
    更新失敗でも AwsContainer.update() が stack 更新まで続行していた。
    """
    handler = object.__new__(LambdaHandler)
    fake_context = type("Ctx", (), {"function_name": "fn"})()
    monkeypatch.setattr(handler, "context", fake_context, raising=False)
    monkeypatch.setattr(handler, "refresh", lambda: None, raising=False)
    seq = ["PROGRESS", "FAILED"]
    state = {"i": -1}
    monkeypatch.setattr(
        "time.sleep", lambda _s: state.__setitem__("i", min(state["i"] + 1, 1))
    )
    monkeypatch.setattr(
        LambdaHandler, "status", property(lambda self: seq[max(state["i"], 0)])
    )
    with pytest.raises(RuntimeError, match="LastUpdateStatus=Failed"):
        handler.wait_update()


def test_wait_update_returns_on_success(monkeypatch):
    """Successful (COMPLETED) では従来どおり正常終了すること"""
    handler = object.__new__(LambdaHandler)
    fake_context = type("Ctx", (), {"function_name": "fn"})()
    monkeypatch.setattr(handler, "context", fake_context, raising=False)
    monkeypatch.setattr(handler, "refresh", lambda: None, raising=False)
    seq = ["PROGRESS", "COMPLETED"]
    state = {"i": -1}
    monkeypatch.setattr(
        "time.sleep", lambda _s: state.__setitem__("i", min(state["i"] + 1, 1))
    )
    monkeypatch.setattr(
        LambdaHandler, "status", property(lambda self: seq[max(state["i"], 0)])
    )
    handler.wait_update()  # raise しないこと


def test_show_logs_raises_on_timeout(monkeypatch):
    """REPORT 行に到達しないまま timeout したら失敗として送出する (false green 防止)。

    以前は print して正常 return し、成功センチネル未観測でも exit 0 になっていた。
    """
    _, _, request_id = _make_handler(monkeypatch, [])
    messages = [
        '"START RequestId: %s"' % request_id,
        "Running migrations...",
    ]
    handler, invoke_response, _ = _make_handler(monkeypatch, messages)
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(ManagementCommandFailed, match="Timeout"):
        handler.show_logs(invoke_response, timeout_seconds=5)


def test_filter_log_messages_follows_next_token(monkeypatch):
    """filter_log_events の nextToken を辿って全件取得すること"""

    class _PagedLogsClient:
        def __init__(self):
            self.calls = []

        def filter_log_events(self, **kwargs):
            self.calls.append(kwargs)
            if "nextToken" not in kwargs:
                return {
                    "events": [{"message": "page1"}],
                    "nextToken": "t1",
                }
            return {"events": [{"message": "page2"}]}

    handler = object.__new__(LambdaHandler)
    fake_context = type("Ctx", (), {"log_group_name": "lg"})()
    monkeypatch.setattr(handler, "context", fake_context, raising=False)
    client = _PagedLogsClient()
    monkeypatch.setattr(handler, "logs_client", client, raising=False)
    messages = handler._filter_log_messages(log_stream_name="s", start_time=0)
    assert messages == ["page1", "page2"]
    assert len(client.calls) == 2
    assert client.calls[1]["nextToken"] == "t1"


def test_update_reframes_resource_conflict(monkeypatch):
    """並行 deploy 等で Lambda が更新中 (ResourceConflictException) のとき、
    生 traceback ではなく原因と次の一手が読める legible error に包み直す。"""

    class _ResourceConflictException(Exception):
        pass

    class _FakeExceptions:
        ResourceConflictException = _ResourceConflictException

    class _FakeLambdaClient:
        exceptions = _FakeExceptions()

        def update_function_code(self, **kwargs):
            raise _ResourceConflictException(
                "An update is in progress for resource: ..."
            )

    handler = object.__new__(LambdaHandler)  # __init__ (boto client 生成) を回避
    fake_context = type("Ctx", (), {"function_name": "sandbox-pocket-wsgi"})()
    monkeypatch.setattr(handler, "context", fake_context, raising=False)
    monkeypatch.setattr(handler, "client", _FakeLambdaClient(), raising=False)

    with pytest.raises(ValueError) as exc:
        handler.update(image_uri="dummy:latest")
    msg = str(exc.value)
    # 原因 (別更新中) と対象、次の一手 (完了を待って再実行) が本文に含まれる
    assert "sandbox-pocket-wsgi" in msg
    assert "別の更新処理中" in msg
    assert "再実行" in msg
