from __future__ import annotations

import datetime
import time
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from ..base import ResourceStatus

if TYPE_CHECKING:
    from ...context import LambdaHandlerContext


class LambdaHandler:
    context: LambdaHandlerContext

    def __init__(self, context: LambdaHandlerContext) -> None:
        self.context = context
        self.client = boto3.client("lambda", region_name=context.region)
        self.logs_client = boto3.client("logs", region_name=self.context.region)

    @property
    def name(self):
        return self.context.function_name

    @property
    def status(self) -> ResourceStatus:
        try:
            function = self.client.get_function(FunctionName=self.name)
        except ClientError:
            return "NOEXIST"
        match function["Configuration"]["LastUpdateStatus"]:
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
            res = boto3.client("sqs").get_queue_url(QueueName=self.context.sqs.name)
            return res["QueueUrl"]

    def update(self, image_uri, wait=False):
        res = self.client.update_function_code(
            FunctionName=self.name, ImageUri=image_uri
        )
        print(f"lambda function {self.name} was updated.")
        show_keys = ["FunctionName", "Timeout", "MemorySize", "Version", "Environment"]
        for key, value in res.items():
            if key in show_keys:
                print(f"  - {key}: {value}")
        if wait:
            self.wait_update()

    def wait_update(self, interval=3, limit=60):
        print(f"waiting lambda fanction {self.name} update.", end="", flush=True)
        for _i in range(limit // interval):
            time.sleep(interval)
            if self.status == "PROGRESS":
                print(".", end="", flush=True)
            else:
                print(f"\nlambda function {self.name} was updated.")
                break
        else:
            raise Exception("Lambda couldn't stop updating. Please check.")

    def invoke(self, payload: str):
        return self.client.invoke(
            FunctionName=self.name, InvocationType="Event", Payload=payload
        )

    def show_logs(self, request_id: str, created_at: datetime.datetime):
        start_pattern = '"START RequestId: %s"' % request_id
        report_prefix = "REPORT RequestId: %s" % request_id
        events = self._find_events(start_pattern, created_at)
        print("Log stream found: %s" % events[0]["logStreamName"])
        printed = []
        timeout_seconds = 120
        sleep_seconds = 5
        for _i in range(timeout_seconds // sleep_seconds):
            res = self.logs_client.filter_log_events(
                logGroupName=self.context.log_group_name,
                logStreamNames=[events[0]["logStreamName"]],
                startTime=events[0]["timestamp"],
            )
            messages = [event["message"] for event in res["events"]]
            if messages[: len(printed)] != printed:
                raise Exception("log stream changed")
            for message in messages[len(printed) :]:
                print(message.strip())
                printed.append(message)
                time.sleep(0.05)
                if message.startswith(report_prefix):
                    return
            time.sleep(sleep_seconds)
        print("Timeout %s seconds. Please check logs in cloudwatch." % timeout_seconds)

    def _get_recent_log_stream_names(self, limit: int):
        res = self.logs_client.describe_log_streams(
            logGroupName=self.context.log_group_name,
            orderBy="LastEventTime",
            descending=True,
            limit=limit,
        )
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
            "Searched %s log stream below..."
            % len(target_log_stream_names)  # pyright: ignore
        )
        for s in target_log_stream_names:  # pyright: ignore
            print("  - %s" % s)
        raise Exception("log stream not found")
