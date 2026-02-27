from __future__ import annotations

import time
from functools import cached_property
from typing import TYPE_CHECKING, Any

import boto3
import yaml
from botocore.exceptions import ClientError
from deepdiff import DeepDiff
from jinja2 import Environment, PackageLoader, select_autoescape

from pocket.resources.base import ResourceStatus

if TYPE_CHECKING:
    from pocket.context import AwsContainerContext, CloudFrontContext
    from pocket.general_context import VpcContext


class Stack:
    template_filename: str

    @property
    def name(self) -> str:
        raise NotImplementedError

    @property
    def export(self) -> dict:
        raise NotImplementedError

    def __init__(self, context: AwsContainerContext | VpcContext | CloudFrontContext):
        self.context = context
        self.client = self.get_client()

    def get_client(self):
        return boto3.client("cloudformation", region_name=self.context.region)

    def _get_resource(self) -> Any:
        return None

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
    def uploaded_template(self) -> str | None:
        try:
            return self.client.get_template(StackName=self.name)["TemplateBody"]
        except ClientError:
            return None

    def clear_status(self):
        if hasattr(self, "description"):
            del self.description
        if hasattr(self, "uploaded_template"):
            del self.uploaded_template

    def wait_status(
        self,
        status: ResourceStatus,
        timeout=300,
        interval=3,
        error_statuses: tuple[ResourceStatus] = ("FAILED",),
    ):
        for i in range(timeout // interval):
            self.clear_status()
            if self.status == status:
                print("")
                return
            if self.status in error_statuses:
                print(self.description)
                raise Exception(
                    f"Stack status is {self.status}. Please check the console."
                )
            if i == 0:
                msg = "Waiting for %s stack status to be %s" % (
                    self.template_filename,
                    status,
                )
                print(msg, end="", flush=True)
            print(".", end="", flush=True)
            time.sleep(interval)
        raise Exception("Timeout is %s seconds" % timeout)

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
            return self.description["DeletionTime"].astimezone()

    @property
    def status_detail(self):
        if not self.description:
            return "NOT_CREATED"
        return self.description["StackStatus"]

    @property
    def exists(self):
        return self.status != "NOEXIST"

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
            loader=PackageLoader("pocket_cli"), autoescape=select_autoescape()
        ).get_template(name=f"cloudformation/{self.template_filename}.yaml")
        original_yaml = template.render(
            stack_name=self.name,
            export=self.export,
            resource=self._get_resource(),
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
        return DeepDiff(
            yaml.safe_load(self.uploaded_template or ""),
            yaml.safe_load(self.yaml),
            ignore_order=True,
        )

    def create(self):
        return self.client.create_stack(
            StackName=self.name, TemplateBody=self.yaml, Capabilities=self.capabilities
        )

    def update(self):
        print("Update stack")
        return self.client.update_stack(
            StackName=self.name, TemplateBody=self.yaml, Capabilities=self.capabilities
        )

    def delete(self):
        return self.client.delete_stack(StackName=self.name)


class CloudFrontKeysStack(Stack):
    context: CloudFrontContext
    template_filename = "cloudfront_keys"

    def __init__(
        self,
        context: CloudFrontContext,
        signing_public_key_pem: str = "",
    ):
        self._signing_public_key_pem = signing_public_key_pem
        super().__init__(context)

    def get_client(self):
        return boto3.client("cloudformation", region_name=self.context.region)

    @property
    def name(self):
        return f"{self.context.slug}-cloudfront-keys"

    @property
    def export(self):
        return {
            "public_key_id": f"{self.context.slug}-public-key-id",
            "key_group_id": f"{self.context.slug}-key-group-id",
        }

    @property
    def yaml(self) -> str:
        from jinja2 import Environment, PackageLoader, select_autoescape

        template = Environment(
            loader=PackageLoader("pocket_cli"), autoescape=select_autoescape()
        ).get_template(name=f"cloudformation/{self.template_filename}.yaml")
        original_yaml = template.render(
            stack_name=self.name,
            export=self.export,
            resource=self._get_resource(),
            signing_public_key_pem=self._signing_public_key_pem,
            **self.context.model_dump(),
        )
        return "\n".join(
            [
                line
                for line in original_yaml.splitlines()
                if line.strip() not in ["#", "# prettier-ignore"]
            ]
        )


class CloudFrontStack(Stack):
    context: CloudFrontContext
    template_filename = "cloudfront"

    def get_client(self):
        return boto3.client("cloudformation", region_name=self.context.region)

    @property
    def name(self):
        return f"{self.context.slug}-cloudfront"

    @property
    def export(self):
        if self.context.signing_key:
            return {"key_group_id": f"{self.context.slug}-key-group-id"}
        return {}

    def _resolve_api_origins(self) -> dict[str, str]:
        """Resolve cross-region CloudFormation exports for API origins.

        CloudFormation Fn::ImportValue only works within the same region.
        Since CloudFront stacks run in us-east-1 but Container stacks may be
        in another region, we resolve the exports at render time and embed
        the domain names as literal values.
        """
        if not self.context.api_origins:
            return {}

        cf = boto3.client("cloudformation", region_name=self.context.s3_region)
        exports: dict[str, str] = {}
        paginator = cf.get_paginator("list_exports")
        for page in paginator.paginate():
            for export in page["Exports"]:
                exports[export["Name"]] = export["Value"]

        resolved: dict[str, str] = {}
        for handler_key, export_name in self.context.api_origins.items():
            if export_name not in exports:
                raise RuntimeError(
                    f"CloudFormation export '{export_name}' not found in region "
                    f"'{self.context.s3_region}'. Deploy the Container stack first."
                )
            resolved[handler_key] = exports[export_name]
        return resolved

    @property
    def yaml(self) -> str:
        from jinja2 import Environment, PackageLoader, select_autoescape

        resolved_api_origins = self._resolve_api_origins()
        template = Environment(
            loader=PackageLoader("pocket_cli"), autoescape=select_autoescape()
        ).get_template(name=f"cloudformation/{self.template_filename}.yaml")
        context_data = self.context.model_dump(exclude={"signing_key", "api_origins"})
        original_yaml = template.render(
            stack_name=self.name,
            export=self.export,
            resource=self._get_resource(),
            signing_key=bool(self.context.signing_key),
            api_origins=resolved_api_origins,
            **context_data,
        )
        return "\n".join(
            [
                line
                for line in original_yaml.splitlines()
                if line.strip() not in ["#", "# prettier-ignore"]
            ]
        )


class ContainerStack(Stack):
    context: AwsContainerContext
    template_filename = "awscontainer"

    @property
    def name(self):
        return f"{self.context.slug}-container"

    @property
    def capabilities(self):
        return ["CAPABILITY_NAMED_IAM"]

    @property
    def export(self):
        if self.context.vpc:
            return VpcStack(self.context.vpc).export
        return {}


class VpcStack(Stack):
    context: VpcContext
    template_filename = "vpc"

    def _get_resource(self):
        from pocket_cli.resources.vpc import Vpc

        return Vpc(self.context)

    @property
    def name(self):
        return f"{self.context.name}-vpc"

    @property
    def export(self):
        return {
            "vpc_id": self.context.name + "-vpc-id",
            "private_subnet_": self.context.name + "-private-subnet-",
            "efs_access_point_arn": self.context.name + "-efs-access-point",
            "efs_security_group": self.context.name + "-efs-security-group",
        }
