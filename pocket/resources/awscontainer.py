from __future__ import annotations

import datetime
import time
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from pocket import context
from pocket.resources.aws.cloudformation import ContainerStack
from pocket.resources.aws.ecr import Ecr
from pocket.resources.base import ResourceStatus

if TYPE_CHECKING:
    from pocket.context import AwsContainerContext


class AwsContainer:
    context: AwsContainerContext

    def __init__(self, context: context.AwsContainerContext) -> None:
        self.context = context
        self.client = boto3.client("lambda", region_name=context.region)

    @property
    def logs_client(self):
        return boto3.client("logs", region_name=self.context.region)

    @property
    def stack(self):
        return ContainerStack(self.context)

    @property
    def updating(self):
        for handler in self.context.handlers.values():
            try:
                function = self.client.get_function(FunctionName=handler.function_name)
            except ClientError:
                continue
            if function["Configuration"]["LastUpdateStatus"] == "InProgress":
                return True
        return False

    @property
    def status(self) -> ResourceStatus:
        if self.stack.status == "COMPLETED" and self.updating:
            return "PROGRESS"
        return self.stack.status

    @property
    def repository(self):
        return Ecr(
            self.context.region,
            self.context.repository_name,
            self.context.deploy_version,
            self.context.dockerfile_path,
            self.context.platform,
        )

    def update_functions(self, wait=False):
        show_keys = ["FunctionName", "Timeout", "MemorySize", "Version", "Environment"]
        for handler in self.context.handlers.values():
            try:
                self.client.get_function(FunctionName=handler.function_name)
            except ClientError:
                print("*** function %s was not found. ***" % handler.function_name)
                print("This function would be created by a cloudformation.")
                continue
            res = self.client.update_function_code(
                FunctionName=handler.function_name,
                ImageUri=self.repository.repository_uri
                + ":"
                + self.context.deploy_version,
            )
            print("function was updated.")
            for key, value in res.items():
                if key in show_keys:
                    print(f"  - {key}: {value}")
        if wait:
            print("waiting lambda update.", end="", flush=True)
            for _i in range(20):
                time.sleep(3)
                if self.updating:
                    print(".", end="", flush=True)
                else:
                    print("\nlambda update completed.")
                    break
            else:
                raise Exception("Lambda couldn't stop updating. Please check.")

    def create(self):
        self.repository.sync()
        print(self.stack.create())

    def update(self):
        self.repository.sync()
        self.update_functions(wait=True)
        if not self.stack.yaml_synced:
            self.stack.update()

    def invoke(self, handler: context.AwslambdaHandlerContext, payload: str):
        return self.client.invoke(
            FunctionName=handler.function_name,
            InvocationType="Event",
            Payload=payload,
        )

    def show_logs(
        self,
        handler: context.AwslambdaHandlerContext,
        request_id: str,
        created_at: datetime.datetime,
    ):
        start_pattern = '"START RequestId: %s"' % request_id
        report_prefix = "REPORT RequestId: %s" % request_id
        events = self._find_events(handler, start_pattern, created_at)
        print("Log stream found: %s" % events[0]["logStreamName"])
        printed = []
        timeout_seconds = 120
        sleep_seconds = 5
        for _i in range(timeout_seconds // sleep_seconds):
            res = self.logs_client.filter_log_events(
                logGroupName=handler.log_group_name,
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

    def _get_recent_log_stream_names(
        self,
        handler: context.AwslambdaHandlerContext,
        limit: int,
    ):
        res = self.logs_client.describe_log_streams(
            logGroupName=handler.log_group_name,
            orderBy="LastEventTime",
            descending=True,
            limit=limit,
        )
        return [s["logStreamName"] for s in res["logStreams"]]

    def _find_events(
        self,
        handler: context.AwslambdaHandlerContext,
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
                log_stream_names
                or self._get_recent_log_stream_names(handler, log_stream_limit)
            )
            kwargs = {
                "logGroupName": handler.log_group_name,
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

    def get_host(self, key: str, handler: context.AwslambdaHandlerContext):
        if handler.apigateway is None:
            return
        if handler.apigateway.domain:
            return handler.apigateway.domain
        apiendpoint_key = key.capitalize() + "ApiEndpoint"
        if self.stack.output and apiendpoint_key in self.stack.output:
            return self.stack.output[apiendpoint_key][len("https://") :]

    @property
    def hosts(self) -> dict[str, str]:
        hosts = {}
        for key, handler in self.context.handlers.items():
            hosts[key] = self.get_host(key, handler)
        return hosts

    @property
    def endpoints(self):
        endpoints = {}
        for key, host in self.hosts.items():
            if host:
                endpoints[key] = "https://%s" % host
            else:
                endpoints[key] = None
        return endpoints

    def get_queueurl(self, handler: context.AwslambdaHandlerContext):
        if handler.sqs is None:
            return
        if handler.sqs.name:
            sqs_client = boto3.client("sqs")
            res = sqs_client.get_queue_url(QueueName=handler.sqs.name)
            return res["QueueUrl"]

    @property
    def queueurls(self) -> dict[str, str]:
        queueurls = {}
        for key, handler in self.context.handlers.items():
            queueurls[key] = self.get_queueurl(handler)
        return queueurls
