from __future__ import annotations

import datetime
import time
from email.utils import parsedate_to_datetime
from functools import cached_property
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field

from pocket.resources.base import ResourceStatus
from pocket.utils import MANAGE_HANDLER_SUCCESS_SENTINEL

if TYPE_CHECKING:
    from pocket.context import LambdaHandlerContext


class ManagementCommandFailed(Exception):
    """management_command_handler の invoke が失敗した (成功センチネルが出ないまま
    Lambda 実行が終わった) ことを示す。CLI を非ゼロ終了させ false green を防ぐ。"""


def _looks_like_init_failure(log_text: str) -> bool:
    """CloudWatch ログが Lambda の INIT フェーズ失敗を示すか判定する。

    INIT で落ちると管理ハンドラのコードは一切走らないため、「アプリの traceback を
    確認して」という誘導は誤り (存在しない traceback を指す)。version 不整合など
    runtime 側の問題を疑うべきケースを切り分けるためのヒューリスティック。
    """
    if "Runtime.Unknown" in log_text:
        return True
    for line in log_text.splitlines():
        if "INIT_REPORT" in line and "Status: error" in line:
            return True
    return False


class Configuration(BaseModel):
    hash: str | None = Field(alias="CodeSha256", default=None)
    last_update_status: str | None = Field(alias="LastUpdateStatus", default=None)


class LambdaHandler:
    context: LambdaHandlerContext

    def __init__(self, context: LambdaHandlerContext) -> None:
        self.context = context
        self.client = boto3.client("lambda", region_name=context.region)
        self.logs_client = boto3.client("logs", region_name=self.context.region)

    @property
    def name(self):
        return self.context.function_name

    @cached_property
    def configuration(self):
        try:
            data = self.client.get_function(FunctionName=self.name)["Configuration"]
            return Configuration(**data)
        except ClientError:
            return Configuration()

    def refresh(self):
        try:
            del self.configuration
        except AttributeError:
            pass

    @property
    def status(self) -> ResourceStatus:
        match self.configuration.last_update_status:
            case None:
                return "NOEXIST"
            case "InProgress":
                return "PROGRESS"
            case "Failed":
                return "FAILED"
            case "Successful":
                return "COMPLETED"
            case _:
                raise Exception("unexpected status")

    @property
    def queueurl(self) -> str | None:
        if self.context.sqs:
            # region 未指定だと AWS_DEFAULT_REGION が別 region の環境で
            # 誤った queue を照会する (同クラスの他 client と同様に明示する)
            sqs = boto3.client("sqs", region_name=self.context.region)
            res = sqs.get_queue_url(QueueName=self.context.sqs.name)
            return res["QueueUrl"]

    def update(self, image_uri, wait=False):
        try:
            res = self.client.update_function_code(
                FunctionName=self.name, ImageUri=image_uri
            )
        except self.client.exceptions.ResourceConflictException as e:
            # 別の操作 (多くは並行 deploy) が同じ Lambda を更新中だと AWS が
            # ResourceConflictException を返す。生 traceback だと「pocket 内部が
            # 落ちた」と誤読され、原因 (自分の並行実行) が本文から読み取れないため、
            # 原因と次の一手が自己完結する legible error に包み直す
            # (ValueError は PocketCLI が「エラー: ...」で拾い exit 1 にする)。
            raise ValueError(
                "Lambda function '%s' が別の更新処理中のため、"
                "code を更新できませんでした。同じ stage の `pocket deploy` を"
                "並行実行していないか確認し、実行中ならその完了を待ってから"
                "再実行してください (deploy は数分かかります)。" % self.name
            ) from e
        print(f"lambda function {self.name} was updated.")
        show_keys = ["FunctionName", "Timeout", "MemorySize", "Version", "Environment"]
        for key, value in res.items():
            if key in show_keys:
                print(f"  - {key}: {value}")
        if wait:
            self.wait_update()

    def get_environment(self) -> dict[str, str]:
        """Lambda の現状 Environment.Variables を取得する (ImportValue / secret は
        解決済みの実値が返る)。function 未作成なら空 dict。"""
        try:
            config = self.client.get_function_configuration(FunctionName=self.name)
        except ClientError:
            return {}
        return dict(config.get("Environment", {}).get("Variables", {}))

    def update_environment(self, env: dict[str, str], wait=True):
        """Environment.Variables を side-channel で直接更新する。

        `update()` は update_function_code (code のみ) で Environment を更新しない。
        deploy 時に CFn を介さず env を同期したい用途 (DEPLOY_HASH 同期 / reload-env)
        で使う。
        """
        self.client.update_function_configuration(
            FunctionName=self.name, Environment={"Variables": env}
        )
        print(f"lambda function {self.name} environment was updated.")
        if wait:
            self.wait_update()

    def wait_update(self, interval=3, limit=60):
        print(f"waiting lambda function {self.name} update.", end="", flush=True)
        for _i in range(limit // interval):
            time.sleep(interval)
            self.refresh()
            if self.status == "PROGRESS":
                print(".", end="", flush=True)
                continue
            if self.status == "FAILED":
                print("")
                raise RuntimeError(
                    f"lambda function {self.name} の更新が失敗しました "
                    "(LastUpdateStatus=Failed)。image URI や設定を確認してください。"
                )
            print(f"\nlambda function {self.name} was updated.")
            break
        else:
            raise Exception("Lambda couldn't stop updating. Please check.")

    def invoke(self, payload: str):
        return self.client.invoke(
            FunctionName=self.name, InvocationType="Event", Payload=payload
        )

    def show_logs(self, invoke_http_response, timeout_seconds=120):
        res = invoke_http_response
        request_id = res["ResponseMetadata"]["RequestId"]
        created_at_rfc1123 = res["ResponseMetadata"]["HTTPHeaders"]["date"]
        created_at = parsedate_to_datetime(created_at_rfc1123)
        print("lambda request_id:", request_id)
        print("lambda created_at:", created_at)
        start_pattern = '"START RequestId: %s"' % request_id
        report_prefix = "REPORT RequestId: %s" % request_id
        events = self._find_events(start_pattern, created_at)
        print("Log stream found: %s" % events[0]["logStreamName"])
        printed = []
        # 成功センチネル (management_command_handler が例外なく完了したときだけ印字)
        # を REPORT 行までに観測できたかで成否を判定する。非同期 invoke では
        # ハンドラの例外が呼び出し側に伝わらないため、ログ経由で判定する。
        success_seen = False
        sleep_seconds = 5
        for _i in range(timeout_seconds // sleep_seconds):
            messages = self._filter_log_messages(
                log_stream_name=events[0]["logStreamName"],
                start_time=events[0]["timestamp"],
            )
            if messages[: len(printed)] != printed:
                raise Exception("log stream changed")
            for message in messages[len(printed) :]:
                print(message.strip())
                printed.append(message)
                if MANAGE_HANDLER_SUCCESS_SENTINEL in message:
                    success_seen = True
                time.sleep(0.05)
                if message.startswith(report_prefix):
                    if not success_seen:
                        if _looks_like_init_failure("\n".join(printed)):
                            raise ManagementCommandFailed(
                                "Lambda が INIT フェーズで失敗しました "
                                "(Runtime.Unknown / INIT_REPORT ... Status: error)。"
                                "これはアプリの例外ではなく runtime 側の問題の可能性が"
                                "高いです (magic-pocket runtime の版不整合を含む)。"
                                "pocket.toml で新しめの機能を使っている場合、"
                                "magic-pocket runtime が古い可能性があります: "
                                "uv add 'magic-pocket[django]>=<CLI と同じ版>'。"
                                "アプリの traceback ではなく上の INIT_REPORT / "
                                "Runtime.Unknown 行を確認してください。"
                            )
                        raise ManagementCommandFailed(
                            "management command handler did not complete successfully "
                            "(no success marker before REPORT). "
                            "上の CloudWatch ログの traceback を確認してください。"
                        )
                    return
            time.sleep(sleep_seconds)
        # timeout を正常 return にすると成功センチネル未観測でも exit 0 になる
        # (docstring の false green 防止の意図に反する) ため raise する
        raise ManagementCommandFailed(
            "Timeout %s seconds: 成功マーカーを観測できませんでした。"
            "CloudWatch のログを確認してください。処理が長い場合は "
            "--timeout-seconds を増やしてください。" % timeout_seconds
        )

    def _filter_log_messages(self, log_stream_name: str, start_time: int) -> list[str]:
        """filter_log_events を nextToken を辿って全件取得する。

        1 レスポンス上限 (1MB / 10k 件) を超えるログでも REPORT 行まで到達
        できるようにする。
        """
        messages: list[str] = []
        kwargs: dict = {
            "logGroupName": self.context.log_group_name,
            "logStreamNames": [log_stream_name],
            "startTime": start_time,
        }
        while True:
            res = self.logs_client.filter_log_events(**kwargs)
            messages += [event["message"] for event in res["events"]]
            next_token = res.get("nextToken")
            if not next_token:
                return messages
            kwargs["nextToken"] = next_token

    def _get_recent_log_stream_names(self, limit: int):
        try:
            res = self.logs_client.describe_log_streams(
                logGroupName=self.context.log_group_name,
                orderBy="LastEventTime",
                descending=True,
                limit=limit,
            )
        except self.logs_client.exceptions.ResourceNotFoundException:
            return []
        return [s["logStreamName"] for s in res["logStreams"]]

    def _find_events(
        self,
        filter_pattern: str,
        created_at: datetime.datetime,
        log_stream_names: list[str] | None = None,
        log_stream_limit: int = 3,
    ):
        for i in range(20):
            if i != 0:
                msg = "Waiting for log stream." if i == 1 else "."
                print(msg, end="", flush=True)
                time.sleep(3)
            target_log_stream_names = (
                log_stream_names or self._get_recent_log_stream_names(log_stream_limit)
            )
            if not target_log_stream_names:
                continue
            kwargs = {
                "logGroupName": self.context.log_group_name,
                "logStreamNames": target_log_stream_names,
                "filterPattern": filter_pattern,
                "startTime": int(created_at.timestamp() * 1000),
            }
            events = []
            for _j in range(10):
                res = self.logs_client.filter_log_events(**kwargs)
                if res["events"]:
                    events += res["events"]
                if "nextToken" in res:
                    kwargs["nextToken"] = res["nextToken"]
                else:
                    break
            if events:
                return events
        print(
            "Searched %s log stream below..." % len(target_log_stream_names)  # pyright: ignore
        )
        for s in target_log_stream_names:  # pyright: ignore
            print("  - %s" % s)
        raise Exception("log stream not found")
