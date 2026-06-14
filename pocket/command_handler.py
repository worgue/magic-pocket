"""SQS イベント駆動の安全な command worker 基盤 ``BaseCommandHandler``.

長時間 job (provisioning 等) を wsgi (request/response) Lambda の中で background
thread + subprocess で動かすと、Lambda が **レスポンス返却後に execution
environment を freeze** するため、thread からの最終ステータス書き込みが freeze 跨ぎで
落ち、状態ストアが ``running`` のまま固着する。これを避けるため、job を「**SQS で
起動する別 Lambda invocation の本体**」として完走させる。worker は thread ではなく
invocation そのものなので freeze の影響を受けず、最後まで走り切って finalize できる。

安全境界:

- 実行ファイルは subclass の :meth:`BaseCommandHandler.build_argv` が固定し、argv は
  **list** で **``shell=False``** で渡す。任意バイナリ / shell injection を不可にする
  (= ``dangerous_shell_handler`` の安全な後継)。「何を実行させてよいか」の公開ポリシーは
  enqueue する側 (認証済み API 等) が argv をどう組むかで決める。
- 出力 / lifecycle の永続化は sink hook (:meth:`on_start` / :meth:`on_output` /
  :meth:`on_finish` / :meth:`on_crash`) に委譲する。基底は永続化先を知らない。

crash 時の挙動:

- 予期せぬ crash (spawn 失敗 / spec 不正 / sink エラー / OOM / timeout 等) で UI が
  永遠に running を読まないよう、:meth:`_run` は ``try/finally`` + ``done_ok`` フラグで
  「正常完了でないまま抜けた」ときだけ :meth:`on_crash` を呼ぶ。``except`` で握りつぶさ
  ないので、例外はそのまま伝播し CloudWatch traceback + DLQ に残る (AGENTS.md
  「曖昧な例外キャッチ禁止」と整合)。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from abc import ABC, abstractmethod


class BaseCommandHandler(ABC):
    """SQS event を受け、argv を invocation 本体として完走させる worker 基盤.

    subclass は最低限 :meth:`build_argv` を実装すればよい。ステータス / 出力を永続化
    したい場合は sink hook (:meth:`on_start` / :meth:`on_output` / :meth:`on_finish` /
    :meth:`on_crash`) を override する (既定は no-op)。

    ``pocket.toml`` の ``[awscontainer.handlers.<key>]`` の ``command`` が、この
    クラスのインスタンス (呼び出し可能) を dotted-path で指すように配線する。
    """

    #: 出力追記中の sink への書き込みを間引く throttle 間隔 (秒)。
    #: 完了時 (:meth:`on_finish`) は throttle に関係なく確実に書く想定。
    throttle: float = 1.0

    def __call__(self, event, context):
        """SQS event source の Lambda entrypoint.

        バッチ内の各 record (= 1 job) を順に完走させる。例外は catch せず伝播させ、
        SQS の redrive / DLQ に委ねる。
        """
        for record in event["Records"]:
            self._run(json.loads(record["body"]))

    def _run(self, spec: dict) -> None:
        """1 job 分のコマンドを完走させ、進捗 / 結果を sink hook 経由で永続化する."""
        self.on_start(spec)
        done_ok = False
        try:
            argv = self.build_argv(spec)
            # PYTHONUNBUFFERED=1: subprocess 側の stdout を即 flush させ、Pipe 経由の
            # block buffering でエラー時にログが消える事故を避ける。
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            proc = subprocess.Popen(  # noqa: S603 shell=False + 制御された引数
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
            if proc.stdout is None:
                raise RuntimeError("subprocess stdout is not available")

            last_flush = 0.0
            for line in proc.stdout:
                now = time.time()
                flush = now - last_flush >= self.throttle
                self.on_output(spec, line.rstrip("\n"), flush=flush)
                if flush:
                    last_flush = now

            proc.wait()
            # 本体内 (= 同一 invocation) での最終 finalize なので freeze の影響を
            # 受けず確実。
            self.on_finish(spec, proc.returncode)
            done_ok = True
        finally:
            if not done_ok:
                # 例外が伝播中 (= worker crash)。UI が failed を読めるよう sink に
                # 記録する。正常 finalize 後 (done_ok=True) はここを通らない。
                self.on_crash(spec, sys.exc_info()[1])
            # 例外は finally を抜けて自然に伝播 (= re-raise) → CloudWatch + DLQ。

    @abstractmethod
    def build_argv(self, spec: dict) -> list[str]:
        """job spec を実行する argv (list[str]) に変換する.

        安全境界はここ: 実行ファイルを自分の CLI に固定し、shell を介さず list で
        渡す。``spec`` の中身 (どの引数を許すか) の検証も必要ならここで行う。
        """
        ...

    # --- sink hooks ---
    # 既定はいずれも stdout への print (= CloudWatch Logs に残る)。subclass が
    # override してステータス / 出力を任意のストア (S3 snapshot 等) に永続化する。
    # build_argv だけ実装した subclass でも、最低限ログは CloudWatch に出る。

    def on_start(self, spec: dict) -> None:
        """job 開始時 (subprocess 起動前)。running 状態の初期化に使う."""
        print(f"start job: {spec}")

    def on_output(self, spec: dict, line: str, *, flush: bool) -> None:
        """出力 1 行ごと。``flush`` が True のとき永続化先へ書き出す想定."""
        print(line)

    def on_finish(self, spec: dict, exit_code: int) -> None:
        """subprocess 完走時 (exit_code は 0 以外もあり得る = job の失敗)."""
        print(f"finished: exit {exit_code}")

    def on_crash(self, spec: dict, exc: BaseException | None) -> None:
        """worker が正常完了せず抜けたとき (crash)。例外はこの後 re-raise される."""
        print(f"crashed: {type(exc).__name__}: {exc}")
