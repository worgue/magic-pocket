"""destroy の旧命名 ([awscontainer] 時代) fallback と Neon 資格情報なし skip (KN1456)。

- 0.29.0 で container stack 名が {slug}-container → {slug}-container-{name} に
  変わったため、移行 deploy を経ずに旧 stack だけ消したいケース (region 移設・廃止)
  では現行命名が NOEXIST で destroy の対象から漏れていた
- [neon] 宣言があるのに NEON_API_KEY が無い VM では status / destroy が
  Neon の解決 (401) で止まっていた。stack の確認・削除に Neon は無関係なので
  warning で skip して続行する
"""

from __future__ import annotations

import json
from unittest import mock

import boto3
import pytest
from moto import mock_aws
from pocket_cli.cli import destroy_cli, status_cli
from pocket_cli.migrations import legacy_ecr_repo_name
from pocket_cli.resources.aws.state import context_resource_prefix
from pocket_cli.resources.neon import Neon

from pocket.context import Context, NeonContext

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


def _neon_context(api_key: str | None) -> NeonContext:
    return NeonContext(
        api_key=api_key,
        project_name="dev-testprj",
        branch_name="dev",
        name="testprj",
        role_name="testprj",
    )


def _never_call_api(*args, **kwargs):
    pytest.fail("NEON_API_KEY 未設定時に Neon API を呼んではいけない")


@mock_aws
def test_destroy_skips_neon_without_credential(use_toml):
    use_toml("tests/data/toml/default.toml")
    context = Context.from_toml(stage="dev").model_copy(
        update={"neon": _neon_context(api_key=None)}
    )
    with mock.patch("pocket.provisioning.neon._http_request", _never_call_api):
        assert destroy_cli._collect_external_database_targets(context) == []
        destroy_cli._destroy_neon(context)


def test_status_skip_reason_for_neon_without_credential():
    assert status_cli.skip_reason(Neon(_neon_context(api_key=None)))
    assert status_cli.skip_reason(Neon(_neon_context(api_key="k"))) is None
