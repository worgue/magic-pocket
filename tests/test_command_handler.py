"""pocket.command_handler.BaseCommandHandler の lifecycle テスト.

実際に subprocess (python -c) を起動し、出力・正常完了・crash 時の挙動と、
crash した record だけを batchItemFailures で報告する partial batch response を
確認する。
"""

import json
import sys

import pytest

from pocket.command_handler import BaseCommandHandler


class RecordingHandler(BaseCommandHandler):
    """sink hook の呼び出しを記録するテスト用 subclass."""

    throttle = 0.0  # 全行を flush=True で受ける

    def __init__(self, argv):
        self._argv = argv
        self.events: list[tuple] = []

    def build_argv(self, spec):
        return self._argv

    def on_start(self, spec):
        self.events.append(("start",))

    def on_output(self, spec, line, *, flush):
        self.events.append(("output", line, flush))

    def on_finish(self, spec, exit_code):
        self.events.append(("finish", exit_code))

    def on_crash(self, spec, exc):
        self.events.append(("crash", type(exc).__name__))


def _record(spec, message_id):
    return {"body": json.dumps(spec), "messageId": message_id}


def _sqs_event(spec, message_id="m1"):
    return {"Records": [_record(spec, message_id)]}


def test_finishes_and_captures_output():
    handler = RecordingHandler([sys.executable, "-c", "print('hello'); print('world')"])
    handler(_sqs_event({"job_id": "j1"}), None)
    assert handler.events[0] == ("start",)
    outputs = [e for e in handler.events if e[0] == "output"]
    assert [o[1] for o in outputs] == ["hello", "world"]
    assert ("finish", 0) in handler.events
    assert not any(e[0] == "crash" for e in handler.events)


def test_nonzero_exit_is_finish_not_crash():
    """job 自体の失敗 (exit != 0) は crash ではなく on_finish で渡る."""
    handler = RecordingHandler([sys.executable, "-c", "import sys; sys.exit(3)"])
    handler(_sqs_event({"job_id": "j2"}), None)
    assert ("finish", 3) in handler.events
    assert not any(e[0] == "crash" for e in handler.events)


class CrashingHandler(RecordingHandler):
    """build_argv で死ぬ = worker crash する handler."""

    def build_argv(self, spec):
        raise ValueError("bad spec")


def test_run_calls_on_crash_then_reraises():
    """_run は on_crash を呼んでから例外を伝播させる (握りつぶさない)."""
    handler = CrashingHandler([])
    with pytest.raises(ValueError, match="bad spec"):
        handler._run({"job_id": "j3"})
    assert ("start",) in handler.events
    assert ("crash", "ValueError") in handler.events
    assert not any(e[0] == "finish" for e in handler.events)


def test_crash_is_reported_as_batch_item_failure_not_raised():
    """__call__ は crash を伝播させず、該当 record を batchItemFailures で報告する.

    ここで例外を伝播させるとバッチ全件が再配信され、成功済み record の job まで
    二重実行される。
    """
    handler = CrashingHandler([])
    response = handler(_sqs_event({"job_id": "j3"}, message_id="m3"), None)
    assert response == {"batchItemFailures": [{"itemIdentifier": "m3"}]}
    assert ("crash", "ValueError") in handler.events


def test_partial_failure_reports_only_the_failed_record():
    """成功 1 + 失敗 1 のバッチで、失敗した messageId だけが返る.

    成功した record は batchItemFailures に載らないので SQS から削除され、
    再配信されない (= 完了済み job が再実行されない)。
    """

    class PerSpecHandler(RecordingHandler):
        def build_argv(self, spec):
            if spec["job_id"] == "bad":
                raise ValueError("bad spec")
            return [sys.executable, "-c", "print('ok')"]

    handler = PerSpecHandler([])
    event = {
        "Records": [
            _record({"job_id": "good"}, "m-good"),
            _record({"job_id": "bad"}, "m-bad"),
        ]
    }
    response = handler(event, None)
    assert response == {"batchItemFailures": [{"itemIdentifier": "m-bad"}]}
    # 成功した record は最後まで走り切っている
    assert ("finish", 0) in handler.events


def test_all_success_returns_empty_batch_item_failures():
    """全件成功なら空 list を返す (SQS は全件成功と解釈する)."""
    handler = RecordingHandler([sys.executable, "-c", "print('ok')"])
    response = handler(_sqs_event({"job_id": "j4"}), None)
    assert response == {"batchItemFailures": []}
