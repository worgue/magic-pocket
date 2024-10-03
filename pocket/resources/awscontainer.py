from __future__ import annotations

from typing import TYPE_CHECKING

import boto3

from pocket.mediator import Mediator

from .. import context
from ..utils import echo
from .aws.cloudformation import ContainerStack
from .aws.ecr import Ecr
from .aws.lambdahandler import LambdaHandler
from .base import ResourceStatus

if TYPE_CHECKING:
    from ..context import AwsContainerContext


class NotCreatedYetError(Exception):
    pass


class NoApiEndpointError(Exception):
    pass


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
        if self.ecr.uri:
            return self.ecr.uri + ":" + self.context.stage

    @property
    def ecr(self):
        return Ecr(
            self.context.region,
            self.context.ecr_name,
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
        handler_status_list: list[ResourceStatus] = [
            handler.status for handler in self.handlers.values()
        ]
        if ("FAILED" in handler_status_list) or (self.stack.status == "FAILED"):
            return "FAILED"
        if ("PROGRESS" in handler_status_list) or (self.stack.status == "PROGRESS"):
            return "PROGRESS"
        if self.stack.status in ["NOEXIST", "REQUIRE_UPDATE"]:
            return self.stack.status
        for handler in self.handlers.values():
            if handler.configuration.hash != self.ecr.image_detail.hash:
                return "REQUIRE_UPDATE"
        return "COMPLETED"

    @property
    def description(self):
        msg = "Create aws cloudformation stack: %s\n" "Create ecr repository: %s" % (
            self.stack.name,
            self.ecr.name,
        )
        if self.context.secretsmanager and self.context.secretsmanager.pocket_secrets:
            msg += (
                "\nCreate secretsmanager pocket managed secrets: %s"
                % self.context.secretsmanager.pocket_key
            )
        return msg

    def _require_acm_manual_upadte(self):
        manual_cert_ref_names = []
        for handler in self.handlers.values():
            apig_c = handler.context.apigateway
            if apig_c and apig_c.domain and not apig_c.create_records:
                manual_cert_ref_names.append(
                    handler.context.cloudformation_cert_ref_name
                )
        yaml_diff = self.stack.yaml_diff
        for ref_name in manual_cert_ref_names:
            if t := yaml_diff.get("type_changes", {}).get("root", {}).get("old_type"):
                if t == "NoneType":
                    return True
            resource = "root['Resources']['%s']" % ref_name
            if resource in yaml_diff.get("dictionary_item_added", []):
                return True
            for changed_resource in yaml_diff.get("values_changed", {}).keys():
                if changed_resource.startswith(resource):
                    return True
        return False

    def show_acm_manual_request(self):
        if self._require_acm_manual_upadte():
            w = echo.warning
            w("You need to request ACM manually to complete stack events.")
            w("See CloudFormation stack log.")
            w("Probably, you need to request dns A record to WsgiRegionalDomainName")

    def deploy_init(self):
        self.ecr.sync()
        if self.context.vpc:
            self.context.vpc.resource.stack.wait_status("COMPLETED")

    def create(self, mediator: Mediator):
        print("Creating secrets ...")
        mediator.ensure_pocket_managed_secrets()
        print("Creating cloudformation stack for awscontainer ...")
        self.show_acm_manual_request()
        self.stack.create()

    def update(self, mediator: Mediator):
        mediator.ensure_pocket_managed_secrets()
        self.show_acm_manual_request()
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
            raise NoApiEndpointError(f"ApiGateway is not defined in {key}")
        if handler.context.apigateway.domain:
            return handler.context.apigateway.domain
        apiendpoint_key = key.capitalize() + "ApiEndpoint"
        if self.stack.output and apiendpoint_key in self.stack.output:
            return self.stack.output[apiendpoint_key][len("https://") :]
        raise NotCreatedYetError(f"ApiGateway endpoint for {key} is not created yet.")

    @property
    def hosts(self) -> dict[str, str | None]:
        data = {}
        for key in self.handlers:
            try:
                data[key] = self.get_host(key)
            except NotCreatedYetError:
                data[key] = None
            except NoApiEndpointError:
                pass
        return data

    @property
    def endpoints(self):
        return {key: f"https://{host}" for key, host in self.hosts.items() if host}

    @property
    def queueurls(self) -> dict[str, str | None]:
        return {key: handler.queueurl for key, handler in self.handlers.items()}
