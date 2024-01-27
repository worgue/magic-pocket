from __future__ import annotations

import time
from functools import cached_property
from typing import TYPE_CHECKING

import boto3
import yaml
from botocore.exceptions import ClientError
from deepdiff import DeepDiff
from jinja2 import Environment, PackageLoader, select_autoescape

from pocket.resources.base import ResourceStatus

if TYPE_CHECKING:
    from pocket import context


class Stack:
    def __init__(self, context: context.Context):
        self.client = boto3.client("cloudformation", region_name=context.region)

    @property
    def capabilities(self):
        return []

    @cached_property
    def description(self):
        try:
            return self.client.describe_stacks(StackName=self.name)["Stacks"][0]
        except ClientError:
            return None

    @cached_property
    def uploaded_template(self):
        try:
            return self.client.get_template(StackName=self.name)["TemplateBody"]
        except ClientError:
            return None

    def clear_status(self):
        if hasattr(self, "description"):
            del self.description
        if hasattr(self, "uploaded_template"):
            del self.uploaded_template

    def wait_status(self, status: ResourceStatus, timeout=60):
        max_iter = 100
        interval = 3
        if (timeout < 0) or ((max_iter * interval) < timeout):
            raise Exception("timeout value is out of range")
        for i in range(max_iter):
            self.clear_status()
            if self.status == status:
                print("")
                return
            if i == 0:
                print("Waiting for stack status to be %s" % status, end="", flush=True)
            print(".", end="", flush=True)
            time.sleep(interval)

    @property
    def output(self) -> dict[str, str] | None:
        if self.description and "Outputs" in self.description:
            return {
                output["OutputKey"]: output["OutputValue"]
                for output in self.description["Outputs"]
            }

    @property
    def deleted_at(self):
        if self.description:
            print(self.description)
            return self.description["DeletionTime"].astimezone()

    @property
    def status_detail(self):
        if not self.description:
            return "NOT_CREATED"
        return self.description["StackStatus"]

    @property
    def status(self) -> ResourceStatus:
        if self.status_detail == "NOT_CREATED":
            return "NOEXIST"
        action = self.status_detail.split("_")[0]
        action_status = self.status_detail.split("_")[-1]
        if action_status == "PROGRESS":
            return "PROGRESS"
        if action_status == "FAILED":
            return "FAILED"
        if action_status == "COMPLETE":
            if action == "DELETE":
                return "NOEXIST"
            elif action == "ROLLBACK":
                if self.deleted_at:
                    return "FAILED"
                return "COMPLETED" if self.yaml_synced else "REQUIRE_UPDATE"
            elif action in {"IMPORT", "REVIEW", "UPDATE", "CREATE"}:
                return "COMPLETED" if self.yaml_synced else "REQUIRE_UPDATE"
        raise Exception("unknown status")

    @property
    def yaml(self) -> str:
        template = Environment(
            loader=PackageLoader("pocket"), autoescape=select_autoescape()
        ).get_template(name=f"cloudformation/{self.template_filename}.yaml")
        original_yaml = template.render(
            stack_name=self.name,
            export=self.export,
            main_context=self.main_context,
            **self.context.model_dump(),
        )
        return "\n".join(
            [
                line
                for line in original_yaml.splitlines()
                if line.strip() not in ["#", "# prettier-ignore"]
            ]
        )

    @property
    def yaml_synced(self):
        if self.yaml_diff == {}:
            return True
        return False

    @property
    def yaml_diff(self):
        if self.uploaded_template:
            return DeepDiff(
                yaml.safe_load(self.uploaded_template or ""),
                yaml.safe_load(self.yaml),
                ignore_order=True,
            )
        return yaml.safe_load(self.yaml)

    def create(self):
        return self.client.create_stack(
            StackName=self.name, TemplateBody=self.yaml, Capabilities=self.capabilities
        )

    def update(self):
        return self.client.update_stack(
            StackName=self.name, TemplateBody=self.yaml, Capabilities=self.capabilities
        )

    def delete(self):
        return self.client.delete_stack(StackName=self.name)

    @property
    def export(self):
        return {}


class ContainerStack(Stack):
    context: context.AwsContainerContext
    template_filename = "awscontainer"

    def __init__(self, context: context.Context):
        super().__init__(context)
        self.main_context = context
        self.context = context.awscontainer

    @property
    def name(self):
        return f"{self.main_context.slug}-container"

    @property
    def capabilities(self):
        return ["CAPABILITY_NAMED_IAM"]


class VpcStack(Stack):
    context: context.VpcContext
    template_filename = "vpc"

    def __init__(self, context: context.Context):
        super().__init__(context)
        self.main_context = context
        self.context = context.vpc

    @property
    def name(self):
        return f"{self.main_context.slug}-vpc"
