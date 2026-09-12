"""VPC 撤去の残存依存検知と削除待機の再開を検証する。"""

from unittest.mock import Mock

import boto3
import pytest
from moto import mock_aws
from pocket_cli.resources.aws.cloudformation import VpcStack
from pocket_cli.resources.rds import Rds
from pocket_cli.resources.vpc import Vpc

from pocket.context import Context
from pocket.general_context import VpcContext


def _vpc():
    return Vpc(VpcContext(ref="test", name="test-pocket", region="ap-northeast-1"))


@mock_aws
def test_external_eni_and_sg_block_deletion(monkeypatch):
    ec2 = boto3.client("ec2", region_name="ap-northeast-1")
    vpc_id = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock="10.0.0.0/24")["Subnet"][
        "SubnetId"
    ]
    sg = ec2.create_security_group(
        VpcId=vpc_id, GroupName="retired-pocket-lambda", Description="old lambda"
    )["GroupId"]
    eni = ec2.create_network_interface(SubnetId=subnet, Groups=[sg])["NetworkInterface"]
    resources = [
        {"ResourceType": "AWS::EC2::VPC", "PhysicalResourceId": vpc_id},
        {"ResourceType": "AWS::EC2::Subnet", "PhysicalResourceId": subnet},
    ]
    monkeypatch.setattr(VpcStack, "resource_summaries", lambda self: resources)
    resource = _vpc()
    dependencies = resource.deletion_dependencies(resource.stack)
    assert len(dependencies) == 2
    assert any(eni["NetworkInterfaceId"] in line for line in dependencies)
    assert any(sg in line for line in dependencies)
    monkeypatch.setattr(VpcStack, "cfn_status", property(lambda self: "FAILED"))
    monkeypatch.setattr(
        VpcStack, "status_detail", property(lambda self: "DELETE_FAILED")
    )
    delete = Mock()
    monkeypatch.setattr(VpcStack, "delete", delete)
    with pytest.raises(RuntimeError, match="スタック外の依存"):
        resource.delete()
    delete.assert_not_called()
    # スタック所有の SG / ENI と default SG は依存扱いにしない。
    resources.extend(
        [
            {"ResourceType": "AWS::EC2::SecurityGroup", "PhysicalResourceId": sg},
            {
                "ResourceType": "AWS::EC2::NetworkInterface",
                "PhysicalResourceId": eni["NetworkInterfaceId"],
            },
        ]
    )
    assert resource.deletion_dependencies(resource.stack) == []


def test_stack_owned_nat_and_efs_enis_are_excluded(monkeypatch):
    stack = Mock()
    stack.resource_summaries.return_value = [
        {"ResourceType": "AWS::EC2::VPC", "PhysicalResourceId": "vpc-owned"},
        {"ResourceType": "AWS::EC2::NatGateway", "PhysicalResourceId": "nat-owned"},
        {"ResourceType": "AWS::EFS::MountTarget", "PhysicalResourceId": "fsmt-owned"},
    ]
    client = Mock()
    pages = {
        "describe_nat_gateways": [
            {
                "NatGateways": [
                    {
                        "NatGatewayId": "nat-owned",
                        "NatGatewayAddresses": [{"NetworkInterfaceId": "eni-nat"}],
                    },
                    {
                        "NatGatewayId": "nat-external",
                        "NatGatewayAddresses": [{"NetworkInterfaceId": "eni-external"}],
                    },
                ]
            }
        ],
        "describe_network_interfaces": [
            {
                "NetworkInterfaces": [
                    {"NetworkInterfaceId": "eni-nat"},
                    {"NetworkInterfaceId": "eni-efs"},
                ]
            },
            {"NetworkInterfaces": [{"NetworkInterfaceId": "eni-external"}]},
        ],
        "describe_security_groups": [{"SecurityGroups": []}],
    }
    client.get_paginator.side_effect = lambda name: Mock(
        paginate=Mock(return_value=pages[name])
    )
    client.describe_mount_targets.return_value = {
        "MountTargets": [{"NetworkInterfaceId": "eni-efs"}]
    }
    monkeypatch.setattr(
        "pocket_cli.resources.vpc.boto3.client", lambda *args, **kwargs: client
    )
    dependencies = _vpc().deletion_dependencies(stack)
    assert len(dependencies) == 1
    assert "eni-external" in dependencies[0]


def test_delete_in_progress_resumes_wait(monkeypatch):
    stack = Mock(cfn_status="PROGRESS", status_detail="DELETE_IN_PROGRESS")
    monkeypatch.setattr(Vpc, "stack", property(lambda self: stack))
    _vpc().delete()
    stack.delete.assert_not_called()
    stack.resource_summaries.assert_not_called()
    stack.wait_status.assert_called_once_with("NOEXIST", timeout=1800, interval=10)


def test_delete_failure_reports_resource_reason(monkeypatch):
    stack = _vpc().stack
    monkeypatch.setattr(VpcStack, "clear_status", lambda self: None)
    stack.description = {
        "StackStatus": "DELETE_FAILED",
        "StackStatusReason": "Subnet deletion failed",
        "Parameters": [{"ParameterValue": "do-not-dump"}],
    }
    monkeypatch.setattr(
        VpcStack,
        "resource_summaries",
        lambda self: [
            {
                "LogicalResourceId": "PrivateSubnet1",
                "PhysicalResourceId": "subnet-1",
                "ResourceStatus": "DELETE_FAILED",
                "ResourceStatusReason": "has dependencies",
            }
        ],
    )
    with pytest.raises(RuntimeError, match="PrivateSubnet1.*has dependencies") as error:
        stack.wait_status("NOEXIST", timeout=1, interval=1)
    assert "do-not-dump" not in str(error.value)


def test_delete_timeout_explains_retry(monkeypatch):
    stack = _vpc().stack
    stack.description = {"StackStatus": "DELETE_IN_PROGRESS"}
    monkeypatch.setattr(VpcStack, "clear_status", lambda self: None)
    monkeypatch.setattr(
        "pocket_cli.resources.aws.cloudformation.time.sleep", lambda _: None
    )
    with pytest.raises(RuntimeError, match="同じ destroy コマンドを再実行"):
        stack.wait_status("NOEXIST", timeout=1, interval=1)


def test_rds_already_deleting_resumes_without_delete_api(use_toml, monkeypatch):
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.rds is not None
    resource = Rds(context.rds)
    resource.instance = {"DBInstanceStatus": "deleting"}
    resource.cluster = {"Status": "deleting"}
    client = Mock()
    monkeypatch.setattr(resource, "_rds_client", client)
    monkeypatch.setattr(Rds, "security_group_id", property(lambda self: None))
    instance_wait = Mock()
    cluster_wait = Mock()
    monkeypatch.setattr(resource, "_wait_instance_deleted", instance_wait)
    monkeypatch.setattr(resource, "_wait_cluster_deleted", cluster_wait)
    resource.delete()
    client.delete_db_instance.assert_not_called()
    client.delete_db_cluster.assert_not_called()
    instance_wait.assert_called_once_with(timeout=1800)
    cluster_wait.assert_called_once_with(timeout=1800)
