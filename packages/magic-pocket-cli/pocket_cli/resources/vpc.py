from __future__ import annotations

from typing import TYPE_CHECKING

import boto3

from pocket.resources.base import ResourceStatus
from pocket_cli.resources.aws.cloudformation import VpcStack
from pocket_cli.resources.aws.efs import Efs

if TYPE_CHECKING:
    from pocket.general_context import VpcContext


class Vpc:
    context: VpcContext

    def __init__(self, context: VpcContext):
        self.context = context
        self.main_context = context

    @property
    def stack(self):
        return VpcStack(self.context)

    @property
    def efs(self):
        if self.context.efs:
            return Efs(self.context.efs)

    @property
    def status(self) -> ResourceStatus:
        return self.stack.status

    @property
    def vpc_id(self):
        if self.stack.output:
            return self.stack.output[self.stack.export["vpc_id"]]

    @property
    def description(self):
        description = "Create aws cloudformation stack: %s" % self.stack.name
        if self.efs:
            description += "\nCreate efs: %s" % self.efs.context.name
        return description

    def state_info(self):
        if self.context.efs:
            return {"efs": {"name": self.context.efs.name}}
        return {}

    def deploy_init(self):
        if self.efs:
            self.efs.ensure_exists()

    def create(self):
        print("Creating cloudformation stack for vpc ...")
        self.stack.create()

    def delete(self):
        stack = self.stack
        if stack.cfn_status != "NOEXIST":
            if stack.status_detail != "DELETE_IN_PROGRESS":
                dependencies = self.deletion_dependencies(stack)
                if dependencies:
                    raise RuntimeError(
                        "VPC の削除を妨げるスタック外の依存があります:\n"
                        + "\n".join(dependencies)
                        + "\n所有元の VPC 離脱・削除と ENI 解放を確認して"
                        "再実行してください。"
                        "available の ENI や pocket 命名の SG でも、名前や状態だけでは"
                        "削除可能と判断できません。所有元・他の利用者を確認してください。"
                    )
                stack.delete()
            stack.wait_status("NOEXIST", timeout=1800, interval=10)
        if self.efs and self.efs.exists():
            self.efs.delete()

    def deletion_dependencies(self, stack: VpcStack) -> list[str]:
        """スタック自身の NAT / EFS を除き、残存 ENI と SG を調べる。"""
        resources = [
            item
            for item in stack.resource_summaries()
            if item.get("ResourceStatus") != "DELETE_COMPLETE"
        ]
        ids = {
            item["PhysicalResourceId"]
            for item in resources
            if item.get("PhysicalResourceId")
        }
        vpc_ids = [
            item["PhysicalResourceId"]
            for item in resources
            if item["ResourceType"] == "AWS::EC2::VPC"
            and item.get("PhysicalResourceId")
        ]
        if not vpc_ids:
            return []
        ec2 = boto3.client("ec2", region_name=self.context.region)
        filters = [{"Name": "vpc-id", "Values": vpc_ids}]
        owned_enis = self._owned_network_interfaces(resources, ids, ec2, filters)
        dependencies = []
        for page in ec2.get_paginator("describe_network_interfaces").paginate(
            Filters=filters
        ):
            for eni in page["NetworkInterfaces"]:
                if eni["NetworkInterfaceId"] not in owned_enis:
                    dependencies.append(
                        f"ENI {eni['NetworkInterfaceId']} "
                        f"type={eni.get('InterfaceType', '-')} "
                        f"status={eni.get('Status', '-')} "
                        f"subnet={eni.get('SubnetId', '-')} "
                        f"description={eni.get('Description', '')}"
                    )
        for page in ec2.get_paginator("describe_security_groups").paginate(
            Filters=filters
        ):
            for group in page["SecurityGroups"]:
                if group["GroupId"] not in ids and group["GroupName"] != "default":
                    dependencies.append(
                        f"SG {group['GroupId']} name={group['GroupName']} "
                        f"description={group.get('Description', '')}"
                    )
        return dependencies

    def update(self):
        if not self.stack.yaml_synced:
            self.stack.update()

    def _owned_network_interfaces(self, resources, ids, ec2, filters) -> set[str]:
        """NAT / EFS の API から、スタックが管理する ENI の ID を解決する。"""
        owned_enis = set(ids)
        for page in ec2.get_paginator("describe_nat_gateways").paginate(Filter=filters):
            for nat in page["NatGateways"]:
                if nat["NatGatewayId"] in ids:
                    owned_enis.update(
                        address["NetworkInterfaceId"]
                        for address in nat.get("NatGatewayAddresses", [])
                        if address.get("NetworkInterfaceId")
                    )
        for item in resources:
            if item["ResourceType"] == "AWS::EFS::MountTarget":
                efs = boto3.client("efs", region_name=self.context.region)
                targets = efs.describe_mount_targets(
                    MountTargetId=item["PhysicalResourceId"]
                )["MountTargets"]
                owned_enis.update(target["NetworkInterfaceId"] for target in targets)
        return owned_enis
