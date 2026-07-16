from __future__ import annotations

import time
from typing import Callable


def wait_until(
    poll: Callable[[], bool],
    *,
    timeout: int,
    interval: int,
    start_message: str,
    timeout_message: str,
    timeout_exc: type[Exception] = TimeoutError,
) -> None:
    """``poll()`` が完了状態で ``True`` を返すまで ``interval`` 秒間隔で待機する。

    ``poll()`` は完了なら ``True``、未完了なら falsy を返す。中断したい場合
    (回復不能なエラー等) は ``poll()`` 内で例外を送出すればそのまま伝播する。

    タイムアウト時は **必ず** ``timeout_exc`` を送出する。時間切れを silent に
    正常 return してしまうと、待機が無意味なまま後続処理へ進んで事故になる。
    silent timeout が必要な呼び出し側は、例外を捕捉して明示的に opt-in すること。

    進捗は 1 行のドット表示 (``start_message`` + ``.....``) で出力する。
    """
    for i in range(timeout // interval):
        if poll():
            print("")
            return
        if i == 0:
            print(start_message, end="", flush=True)
        print(".", end="", flush=True)
        time.sleep(interval)
    raise timeout_exc(timeout_message)
