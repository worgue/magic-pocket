"""pocket.command_handler.BaseCommandHandler の lifecycle テスト.

実際に subprocess (python -c) を起動し、出力・正常完了・crash 時の挙動を確認する。
"""

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


def _sqs_event(spec):
    import json

    return {"Records": [{"body": json.dumps(spec)}]}


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


def test_crash_calls_on_crash_then_reraises():
    """build_argv で死ぬ worker crash は on_crash を呼んでから例外を伝播させる."""

    class CrashingHandler(RecordingHandler):
        def build_argv(self, spec):
            raise ValueError("bad spec")

    handler = CrashingHandler([])
    with pytest.raises(ValueError, match="bad spec"):
        handler(_sqs_event({"job_id": "j3"}), None)
    assert ("start",) in handler.events
    assert ("crash", "ValueError") in handler.events
    assert not any(e[0] == "finish" for e in handler.events)
