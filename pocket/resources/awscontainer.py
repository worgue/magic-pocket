from __future__ import annotations

from typing import TYPE_CHECKING

import boto3

from pocket.mediator import Mediator

from .. import context
from .aws.cloudformation import ContainerStack
from .aws.ecr import Ecr
from .aws.lambdahandler import LambdaHandler
from .base import ResourceStatus

if TYPE_CHECKING:
    from ..context import AwsContainerContext


class AwsContainer:
    """This is abstructed resource to run container in aws.
    This class depends on aws resources.
    """

    context: AwsContainerContext

    def __init__(self, context: context.AwsContainerContext) -> None:
        self.context = context
        self.client = boto3.client("lambda", region_name=context.region)

    @property
    def image_uri(self):
        if self.repository.uri:
            return self.repository.uri + ":" + self.context.stage

    @property
    def repository(self):
        return Ecr(
            self.context.region,
            self.context.repository_name,
            self.context.stage,
            self.context.dockerfile_path,
            self.context.platform,
        )

    @property
    def stack(self):
        return ContainerStack(self.context)

    @property
    def handlers(self):
        handlers: dict[str, LambdaHandler] = {}
        for key, handler in self.context.handlers.items():
            handlers[key] = LambdaHandler(handler)
        return handlers

    @property
    def handlers_updating(self):
        return any(handler.status == "PROGRESS" for handler in self.handlers.values())

    @property
    def status(self) -> ResourceStatus:
        status_list: list[ResourceStatus] = [
            handler.status for handler in self.handlers.values()
        ]
        status_list.append(self.stack.status)
        for status in ["NOEXIST", "FAILED", "PROGRESS", "REQUIRE_UPDATE"]:
            if status in status_list:
                return status
        for handler in self.handlers.values():
            if handler.configuration.hash != self.repository.image_detail.hash:
                return "REQUIRE_UPDATE"
        return "COMPLETED"

    @property
    def description(self):
        msg = "Create aws cloudformation stack: %s\n" "Create ecr repository: %s" % (
            self.stack.name,
            self.repository.name,
        )
        if self.context.secretsmanager and self.context.secretsmanager.pocket_secrets:
            msg += (
                "\nCreate secretsmanager pocket managed secrets: %s"
                % self.context.secretsmanager.pocket_key
            )
        return msg

    def deploy_init(self):
        self.repository.sync()
        if self.context.vpc:
            self.context.vpc.resource.stack.wait_status("COMPLETED")

    def create(self, mediator: Mediator):
        print("Creating secrets ...")
        mediator.ensure_pocket_managed_secrets()
        print("Creating cloudformation stack for awscontainer ...")
        self.stack.create()

    def update(self, mediator: Mediator):
        mediator.ensure_pocket_managed_secrets()
        for key, handler in self.handlers.items():
            if handler.status == "NOEXIST":
                print(f"function {key} was not found and skipped.")
            else:
                handler.update(image_uri=self.image_uri)
        for handler in self.handlers.values():
            handler.wait_update()
        if not self.stack.yaml_synced:
            self.stack.update()

    def get_host(self, key: str):
        handler = self.handlers[key]
        if handler.context.apigateway is None:
            return
        if handler.context.apigateway.domain:
            return handler.context.apigateway.domain
        apiendpoint_key = key.capitalize() + "ApiEndpoint"
        if self.stack.output and apiendpoint_key in self.stack.output:
            return self.stack.output[apiendpoint_key][len("https://") :]

    @property
    def hosts(self) -> dict[str, str | None]:
        return {key: self.get_host(key) for key in self.handlers}

    def get_endpoint(self, key: str):
        host = self.get_host(key)
        if host:
            return f"https://{host}"

    @property
    def endpoints(self):
        return {key: self.get_endpoint(key) for key in self.handlers}

    @property
    def queueurls(self) -> dict[str, str | None]:
        return {key: handler.queueurl for key, handler in self.handlers.items()}
