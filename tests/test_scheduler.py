import json

import pytest
from moto import mock_aws

from pocket.context import Context
from pocket.settings import (
    DjangoManagementScheduleEntry,
    LambdaScheduleEntry,
    Scheduler,
    Settings,
)


@mock_aws
def test_scheduler_dev_loads_global_schedules(use_toml):
    use_toml("tests/data/toml/scheduler.toml")
    context = Context.from_toml(stage="dev")
    assert context.scheduler is not None
    keys = [e.key for e in context.scheduler.schedules]
    assert sorted(keys) == ["daily_digest", "rotate_logs"]
    by_key = {e.key: e for e in context.scheduler.schedules}
    rotate = by_key["rotate_logs"]
    assert rotate.schedule_expression == "rate(1 hour)"
    assert rotate.scheduler == "pocket.lambda_scheduler"
    assert rotate.handler == "worker"
    assert json.loads(rotate.input_json) == {"task": "rotate_logs"}
    assert rotate.yaml_key == "RotateLogs"
    assert rotate.name == "dev-testprj-pocket-rotate-logs"
    digest = by_key["daily_digest"]
    assert digest.schedule_expression == "cron(0 18 * * ? *)"
    assert digest.is_django_management
    assert json.loads(digest.input_json) == {"manage": "send_daily_digest --verbose"}


@mock_aws
def test_scheduler_prod_deep_merges_overrides(use_toml):
    """prod では rotate_logs の rate が上書きされ、month_end が追加される"""
    use_toml("tests/data/toml/scheduler.toml")
    context = Context.from_toml(stage="prod")
    assert context.scheduler is not None
    by_key = {e.key: e for e in context.scheduler.schedules}
    assert sorted(by_key) == ["daily_digest", "month_end", "rotate_logs"]
    assert by_key["rotate_logs"].schedule_expression == "rate(10 minutes)"
    # daily_digest はグローバル定義のままマージされる
    assert by_key["daily_digest"].is_django_management
    month_end = by_key["month_end"]
    assert month_end.is_django_management
    assert json.loads(month_end.input_json) == {"manage": "send_monthly_invoice"}


@mock_aws
def test_scheduler_invoked_function_arns(use_toml):
    use_toml("tests/data/toml/scheduler.toml")
    context = Context.from_toml(stage="dev")
    assert context.scheduler is not None
    arns = context.scheduler.invoked_function_arns
    assert any("management" in arn for arn in arns)
    assert any("worker" in arn for arn in arns)


def test_lambda_schedule_entry_requires_cron_or_rate():
    with pytest.raises(ValueError, match="exactly one of cron / rate"):
        LambdaScheduleEntry.model_validate({"handler": "worker"})
    with pytest.raises(ValueError, match="exactly one of cron / rate"):
        LambdaScheduleEntry.model_validate(
            {"handler": "worker", "cron": "0 * * * ? *", "rate": "1 hour"}
        )


def test_django_management_entry_requires_non_empty_manage():
    with pytest.raises(ValueError, match="non-empty"):
        DjangoManagementScheduleEntry.model_validate(
            {
                "scheduler": "pocket.django.management_lambda_scheduler",
                "rate": "1 day",
                "handler": "management",
                "manage": "   ",
            }
        )


def test_scheduler_default_scheduler_field_is_lambda():
    """scheduler フィールドを省略すると lambda_scheduler になる"""
    s = Scheduler.model_validate(
        {
            "schedules": {
                "hourly": {"rate": "1 hour", "handler": "worker"},
            }
        }
    )
    entry = s.schedules["hourly"]
    assert isinstance(entry, LambdaScheduleEntry)
    assert entry.scheduler == "pocket.lambda_scheduler"


def test_scheduler_unknown_handler_rejected(use_toml):
    """schedule entry が存在しない handler を参照するとエラー"""
    use_toml("tests/data/toml/scheduler.toml")
    # 直接 Settings を組んで負例を作る
    with pytest.raises(ValueError, match="not found in awscontainer.handlers"):
        Settings.model_validate(
            {
                "stage": "dev",
                "general": {
                    "region": "ap-southeast-1",
                    "project_name": "testprj",
                    "stages": ["dev"],
                },
                "s3": {},
                "awscontainer": {
                    "dockerfile_path": "tests/sampleprj/Dockerfile",
                    "handlers": {
                        "worker": {
                            "command": "pocket.lambda_handlers.worker_handler",
                        },
                    },
                },
                "scheduler": {
                    "schedules": {
                        "ghost": {"rate": "1 hour", "handler": "no_such_handler"},
                    }
                },
            }
        )


def test_django_management_requires_management_handler():
    """django_management scheduler は management_command_handler を要求する"""
    with pytest.raises(ValueError, match="management_command_handler"):
        Settings.model_validate(
            {
                "stage": "dev",
                "general": {
                    "region": "ap-southeast-1",
                    "project_name": "testprj",
                    "stages": ["dev"],
                },
                "s3": {},
                "awscontainer": {
                    "dockerfile_path": "tests/sampleprj/Dockerfile",
                    "handlers": {
                        "worker": {
                            "command": "pocket.lambda_handlers.worker_handler",
                        },
                    },
                },
                "scheduler": {
                    "schedules": {
                        "nightly": {
                            "scheduler": "pocket.django.management_lambda_scheduler",
                            "cron": "0 18 * * ? *",
                            "handler": "worker",
                            "manage": "send_daily_digest",
                        },
                    }
                },
            }
        )


@mock_aws
def test_scheduler_cfn_template_renders(use_toml):
    """awscontainer.yaml に Scheduler リソース + Role が出力されること"""
    use_toml("tests/data/toml/scheduler.toml")
    context = Context.from_toml(stage="prod")
    from pocket_cli.resources.aws.cloudformation import ContainerStack

    assert context.awscontainer
    stack = ContainerStack(
        context.awscontainer,
        scheduler_context=context.scheduler,
    )
    yaml = stack.yaml
    # 共有 IAM Role
    assert "SchedulerExecutionRole:" in yaml
    assert "scheduler.amazonaws.com" in yaml
    # 各 schedule resource (logical ID は CamelCase で quote される)
    assert '"RotateLogsSchedule"' in yaml
    assert '"DailyDigestSchedule"' in yaml
    assert '"MonthEndSchedule"' in yaml
    # ScheduleExpression と Input が含まれる
    assert "rate(10 minutes)" in yaml
    assert "cron(0 18 * * ? *)" in yaml
    assert "cron(0 0 L * ? *)" in yaml
    # Input は JSON 文字列として埋め込まれる (YAML 上は backslash escape される)
    assert "send_daily_digest --verbose" in yaml
    assert "send_monthly_invoice" in yaml
    assert "rotate_logs" in yaml
    assert "manage" in yaml
    assert "task" in yaml


@mock_aws
def test_scheduler_cfn_template_absent_when_no_scheduler(use_toml):
    """scheduler 未設定なら SchedulerExecutionRole は出力されない"""
    import boto3

    boto3.client("route53").create_hosted_zone(
        Name="project.com.", CallerReference="test"
    )
    use_toml("tests/data/toml/default.toml")
    context = Context.from_toml(stage="dev")
    from pocket_cli.resources.aws.cloudformation import ContainerStack

    assert context.awscontainer
    assert context.scheduler is None
    stack = ContainerStack(context.awscontainer, scheduler_context=None)
    yaml = stack.yaml
    assert "SchedulerExecutionRole" not in yaml
    assert "AWS::Scheduler::Schedule" not in yaml


def test_management_handler_manage_branch_source():
    """management_command_handler の manage 分岐がソースに含まれていること。

    pocket.django.lambda_handlers は Django app の起動を伴うため
    ユニットテストでは直接 import できない。代わりに source を読んで、
    manage キー → shlex.split + call_command の経路が存在することを確認する。
    """
    from pathlib import Path

    src = Path("pocket/django/lambda_handlers.py").read_text()
    assert '"manage" in event' in src
    assert "shlex.split" in src
    assert "call_command(*tokens)" in src
