"""destroy の旧命名 ([awscontainer] 時代) fallback。

0.29.0 で container stack 名が {slug}-container → {slug}-container-{name} に
変わったため、移行 deploy を経ずに旧 stack だけ消したいケース (region 移設・廃止)
では現行命名が NOEXIST で destroy の対象から漏れていた
"""

from __future__ import annotations

import json

import boto3
from moto import mock_aws
from pocket_cli.cli import destroy_cli
from pocket_cli.migrations import legacy_ecr_repo_name
from pocket_cli.resources.aws.state import context_resource_prefix

from pocket.context import Context

REGION = "ap-southeast-1"
LEGACY_STACK = "dev-testprj-container"


def _create_legacy_resources(context: Context):
    cfn = boto3.client("cloudformation", region_name=REGION)
    cfn.create_stack(
        StackName=LEGACY_STACK,
        TemplateBody=json.dumps(
            {"Resources": {"Q": {"Type": "AWS::SQS::Queue", "Properties": {}}}}
        ),
    )
    ecr = boto3.client("ecr", region_name=REGION)
    ecr.create_repository(repositoryName=legacy_ecr_repo_name(context))
    logs = boto3.client("logs", region_name=REGION)
    logs.create_log_group(
        logGroupName=f"/aws/lambda/{context_resource_prefix(context)}wsgi"
    )


@mock_aws
def test_legacy_container_stack_is_collected_and_destroyed(use_toml):
    use_toml("tests/data/toml/default.toml")
    context = Context.from_toml(stage="dev")
    assert destroy_cli._collect_legacy_container_targets(context) == []

    _create_legacy_resources(context)

    targets = destroy_cli._collect_legacy_container_targets(context)
    assert len(targets) == 2
    assert LEGACY_STACK in targets[0]
    assert legacy_ecr_repo_name(context) in targets[1]

    destroy_cli._destroy_legacy_containers(context)

    assert destroy_cli._collect_legacy_container_targets(context) == []
    logs = boto3.client("logs", region_name=REGION)
    prefix = context_resource_prefix(context)
    assert (
        logs.describe_log_groups(logGroupNamePrefix=f"/aws/lambda/{prefix}")[
            "logGroups"
        ]
        == []
    )


@mock_aws
def test_destroy_legacy_is_noop_without_legacy_resources(use_toml):
    use_toml("tests/data/toml/default.toml")
    context = Context.from_toml(stage="dev")
    destroy_cli._destroy_legacy_containers(context)  # 例外にならない
