from __future__ import annotations

import time
from functools import cached_property
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from pocket.resources.base import ResourceStatus
from pocket.utils import echo
from pocket_cli.resources.aws.cloudformation import VpcStack
from pocket_cli.resources.vpc import Vpc

if TYPE_CHECKING:
    from pocket.context import RdsContext


class RdsResourceIsNotReady(Exception):
    pass


class Rds:
    context: RdsContext

    def __init__(self, context: RdsContext) -> None:
        self.context = context
        self._rds_client = boto3.client("rds", region_name=context.region)
        self._ec2_client = boto3.client("ec2", region_name=context.region)
        self._sm_client = boto3.client("secretsmanager", region_name=context.region)

    @cached_property
    def cluster(self) -> dict | None:
        try:
            res = self._rds_client.describe_db_clusters(
                DBClusterIdentifier=self.context.cluster_identifier
            )
            clusters = res["DBClusters"]
            if clusters:
                return clusters[0]
            return None
        except ClientError as e:
            if e.response["Error"]["Code"] == "DBClusterNotFoundFault":
                return None
            raise

    @cached_property
    def instance(self) -> dict | None:
        try:
            res = self._rds_client.describe_db_instances(
                DBInstanceIdentifier=self.context.instance_identifier
            )
            instances = res["DBInstances"]
            if instances:
                return instances[0]
            return None
        except ClientError as e:
            if e.response["Error"]["Code"] == "DBInstanceNotFound":
                return None
            raise

    @cached_property
    def _security_group(self) -> dict | None:
        res = self._ec2_client.describe_security_groups(
            Filters=[
                {"Name": "tag:Name", "Values": [self.context.security_group_name]},
            ]
        )
        groups = res["SecurityGroups"]
        if groups:
            return groups[0]
        return None

    @property
    def security_group_id(self) -> str | None:
        if self._security_group:
            return self._security_group["GroupId"]
        return None

    @property
    def master_user_secret_arn(self) -> str | None:
        if self.cluster and "MasterUserSecret" in self.cluster:
            return self.cluster["MasterUserSecret"]["SecretArn"]
        return None

    @property
    def status(self) -> ResourceStatus:
        if self.cluster is None:
            return "NOEXIST"
        cluster_status = self.cluster["Status"]
        if cluster_status in ("creating", "modifying", "deleting"):
            return "PROGRESS"
        if cluster_status == "available":
            if self._scaling_config_matches():
                return "COMPLETED"
            return "REQUIRE_UPDATE"
        return "FAILED"

    @property
    def description(self):
        return (
            "Create Aurora PostgreSQL Serverless v2 cluster: %s"
            % self.context.cluster_identifier
        )

    def _scaling_config_matches(self) -> bool:
        if not self.cluster:
            return False
        config = self.cluster.get("ServerlessV2ScalingConfiguration", {})
        return (
            config.get("MinCapacity") == self.context.min_capacity
            and config.get("MaxCapacity") == self.context.max_capacity
        )

    def _get_vpc_stack(self) -> VpcStack:
        return VpcStack(self.context.vpc)

    def _get_vpc_subnet_ids(self) -> list[str]:
        vpc_stack = self._get_vpc_stack()
        output = vpc_stack.output
        export = vpc_stack.export
        assert output, "VPC stack output is not available"
        prefix = export["private_subnet_"]
        subnet_ids = []
        for i in range(1, 20):
            key = f"{prefix}{i}"
            if key in output:
                subnet_ids.append(output[key])
            else:
                break
        assert subnet_ids, "No private subnets found in VPC stack"
        return subnet_ids

    def _get_vpc_id(self) -> str:
        vpc_stack = self._get_vpc_stack()
        output = vpc_stack.output
        export = vpc_stack.export
        assert output, "VPC stack output is not available"
        return output[export["vpc_id"]]

    def state_info(self):
        return {
            "rds": {
                "cluster_identifier": self.context.cluster_identifier,
                "security_group_id": self.security_group_id,
            }
        }

    def deploy_init(self):
        vpc_stack = Vpc(self.context.vpc).stack
        if not self.context.vpc.manage:
            if vpc_stack.status == "NOEXIST":
                raise ValueError(
                    f"外部 VPC スタック '{vpc_stack.name}' が見つかりません。"
                )
        else:
            vpc_stack.wait_status("COMPLETED")

    def create(self):
        subnet_ids = self._get_vpc_subnet_ids()
        vpc_id = self._get_vpc_id()

        # 1. DB Subnet Group
        echo.log("Creating DB Subnet Group: %s" % self.context.subnet_group_name)
        self._rds_client.create_db_subnet_group(
            DBSubnetGroupName=self.context.subnet_group_name,
            DBSubnetGroupDescription="Aurora subnet group for %s"
            % self.context.cluster_identifier,
            SubnetIds=subnet_ids,
            Tags=[{"Key": "Name", "Value": self.context.subnet_group_name}],
        )

        # 2. Security Group
        echo.log("Creating Security Group: %s" % self.context.security_group_name)
        sg_res = self._ec2_client.create_security_group(
            GroupName=self.context.security_group_name,
            Description="RDS Aurora security group for %s"
            % self.context.cluster_identifier,
            VpcId=vpc_id,
            TagSpecifications=[
                {
                    "ResourceType": "security-group",
                    "Tags": [
                        {"Key": "Name", "Value": self.context.security_group_name}
                    ],
                }
            ],
        )
        sg_id = sg_res["GroupId"]

        # 3. Aurora クラスター作成
        echo.log("Creating Aurora cluster: %s" % self.context.cluster_identifier)
        self._rds_client.create_db_cluster(
            DBClusterIdentifier=self.context.cluster_identifier,
            Engine="aurora-postgresql",
            EngineMode="provisioned",
            DatabaseName=self.context.database_name,
            MasterUsername=self.context.master_username,
            ManageMasterUserPassword=True,
            DBSubnetGroupName=self.context.subnet_group_name,
            VpcSecurityGroupIds=[sg_id],
            ServerlessV2ScalingConfiguration={
                "MinCapacity": self.context.min_capacity,
                "MaxCapacity": self.context.max_capacity,
            },
            Tags=[{"Key": "Name", "Value": self.context.cluster_identifier}],
        )

        # 4. Aurora インスタンス作成
        echo.log("Creating Aurora instance: %s" % self.context.instance_identifier)
        self._rds_client.create_db_instance(
            DBInstanceIdentifier=self.context.instance_identifier,
            DBClusterIdentifier=self.context.cluster_identifier,
            DBInstanceClass="db.serverless",
            Engine="aurora-postgresql",
            Tags=[{"Key": "Name", "Value": self.context.instance_identifier}],
        )

        # 5. クラスター available を待機（最大30分）
        echo.log("Waiting for Aurora cluster to become available...")
        self._wait_cluster_available(timeout=1800)
        echo.success("Aurora cluster is now available.")

    def update(self):
        if self._scaling_config_matches():
            echo.log("RDS scaling configuration is up to date.")
            return
        echo.log("Updating RDS scaling configuration...")
        self._rds_client.modify_db_cluster(
            DBClusterIdentifier=self.context.cluster_identifier,
            ServerlessV2ScalingConfiguration={
                "MinCapacity": self.context.min_capacity,
                "MaxCapacity": self.context.max_capacity,
            },
        )
        echo.success("RDS scaling configuration updated.")

    def delete(self):
        # 1. インスタンス削除
        if self.instance:
            echo.log("Deleting Aurora instance: %s" % self.context.instance_identifier)
            self._rds_client.delete_db_instance(
                DBInstanceIdentifier=self.context.instance_identifier,
                SkipFinalSnapshot=True,
            )
            self._wait_instance_deleted(timeout=600)
            echo.success("Aurora instance deleted.")

        # 2. クラスター削除（FinalSnapshot 付き）
        if self.cluster:
            snapshot_id = "%s-final-%s" % (
                self.context.cluster_identifier,
                int(time.time()),
            )
            echo.log("Deleting Aurora cluster: %s" % self.context.cluster_identifier)
            self._rds_client.delete_db_cluster(
                DBClusterIdentifier=self.context.cluster_identifier,
                SkipFinalSnapshot=False,
                FinalDBSnapshotIdentifier=snapshot_id,
            )
            self._wait_cluster_deleted(timeout=600)
            echo.success("Aurora cluster deleted. Final snapshot: %s" % snapshot_id)

        # 3. Security Group 削除
        if self.security_group_id:
            echo.log("Deleting Security Group: %s" % self.context.security_group_name)
            self._ec2_client.delete_security_group(GroupId=self.security_group_id)
            echo.success("Security Group deleted.")

        # 4. Subnet Group 削除
        try:
            echo.log("Deleting DB Subnet Group: %s" % self.context.subnet_group_name)
            self._rds_client.delete_db_subnet_group(
                DBSubnetGroupName=self.context.subnet_group_name
            )
            echo.success("DB Subnet Group deleted.")
        except ClientError as e:
            if e.response["Error"]["Code"] != "DBSubnetGroupNotFoundFault":
                raise

    def _wait_cluster_available(self, timeout: int = 1800, interval: int = 10):
        for i in range(timeout // interval):
            try:
                res = self._rds_client.describe_db_clusters(
                    DBClusterIdentifier=self.context.cluster_identifier
                )
                status = res["DBClusters"][0]["Status"]
                if status == "available":
                    print("")
                    return
            except ClientError:
                pass
            if i == 0:
                print("Waiting for cluster to be available", end="", flush=True)
            print(".", end="", flush=True)
            time.sleep(interval)
        raise TimeoutError(
            "Cluster did not become available within %s seconds" % timeout
        )

    def _wait_instance_deleted(self, timeout: int = 600, interval: int = 10):
        for i in range(timeout // interval):
            try:
                self._rds_client.describe_db_instances(
                    DBInstanceIdentifier=self.context.instance_identifier
                )
            except ClientError as e:
                if e.response["Error"]["Code"] == "DBInstanceNotFound":
                    print("")
                    return
                raise
            if i == 0:
                print("Waiting for instance deletion", end="", flush=True)
            print(".", end="", flush=True)
            time.sleep(interval)
        raise TimeoutError("Instance not deleted within %s seconds" % timeout)

    def _wait_cluster_deleted(self, timeout: int = 600, interval: int = 10):
        for i in range(timeout // interval):
            try:
                res = self._rds_client.describe_db_clusters(
                    DBClusterIdentifier=self.context.cluster_identifier
                )
                status = res["DBClusters"][0]["Status"]
                if status == "deleting":
                    pass
            except ClientError as e:
                if e.response["Error"]["Code"] == "DBClusterNotFoundFault":
                    print("")
                    return
                raise
            if i == 0:
                print("Waiting for cluster deletion", end="", flush=True)
            print(".", end="", flush=True)
            time.sleep(interval)
        raise TimeoutError("Cluster not deleted within %s seconds" % timeout)

    def clear_cache(self):
        for attr in ("cluster", "instance", "_security_group"):
            if attr in self.__dict__:
                del self.__dict__[attr]
