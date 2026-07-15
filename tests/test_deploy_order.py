"""deploy_init / deploy_resources の順序に関するテスト。

VPC + RDS 構成で、deploy_init 時に VPC がまだ作成されていなくても
ハングしないことを検証する。
"""

import pytest
from moto import mock_aws
from pocket_cli.resources.awscontainer import AwsContainer
from pocket_cli.resources.rds import Rds
from pocket_cli.resources.vpc import Vpc

from pocket.context import Context


@mock_aws
def test_awscontainer_deploy_init_does_not_hang_without_vpc(use_toml, monkeypatch):
    """VPC が未作成でも AwsContainer.deploy_init() がハングしないことを確認"""
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.awscontainer is not None
    assert context.awscontainer.vpc is not None

    # deploy_init の副作用をスキップ
    monkeypatch.setattr(
        "pocket_cli.resources.awscontainer.generate_runtime_config",
        lambda _: None,
    )
    monkeypatch.setattr(
        "pocket_cli.resources.aws.ecr.Ecr.sync",
        lambda self: None,
    )

    ac = AwsContainer(context.awscontainer)
    # VPC スタックが存在しない状態で deploy_init を呼ぶ
    # 以前のコードでは vpc_stack.wait_status("COMPLETED") でハングしていた
    ac.deploy_init()  # ハングせずに完了すること


@mock_aws
def test_rds_deploy_init_does_not_hang_without_vpc(use_toml):
    """VPC が未作成でも Rds.deploy_init() がハングしないことを確認"""
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.rds is not None

    rds = Rds(context.rds)

    # VPC スタックが存在しない状態で deploy_init を呼ぶ
    # 以前のコードでは vpc_stack.wait_status("COMPLETED") でハングしていた
    rds.deploy_init()  # ハングせずに完了すること


@mock_aws
def test_vpc_create_then_rds_create_succeeds(use_toml):
    """VPC create → RDS create の順序で VPC 待機が正しく動くことを確認"""
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.rds is not None
    assert context.awscontainer is not None
    assert context.awscontainer.vpc is not None

    vpc = Vpc(context.awscontainer.vpc)

    # VPC スタックを作成（moto では即座に CREATE_COMPLETE になる）
    vpc.create()

    # VPC スタックが COMPLETED であることを確認
    vpc.stack.clear_status()
    assert vpc.stack.status == "COMPLETED"


@mock_aws
def test_wait_status_noexist_raises_when_expecting_completed(use_toml):
    """COMPLETED を待っているのにスタックが NOEXIST のままなら
    タイムアウトではなく早期にエラーになることを確認"""
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.awscontainer is not None
    assert context.awscontainer.vpc is not None

    vpc = Vpc(context.awscontainer.vpc)

    # VPC スタックが存在しない状態で wait_status("COMPLETED") を呼ぶ
    with pytest.raises(RuntimeError, match="見つかりません"):
        vpc.stack.wait_status("COMPLETED", timeout=15, interval=1)


@mock_aws
def test_wait_status_raises_on_rollback_transition(use_toml, monkeypatch):
    """更新開始後に ROLLBACK へ遷移したら wait_status("COMPLETED") が失敗すること

    UPDATE_ROLLBACK_COMPLETE は cfn_status では COMPLETED (安定状態で再 update 可能)
    のため、遷移を検出しないと更新失敗が成功扱いになる (false green の回帰テスト)。
    """
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.awscontainer is not None
    assert context.awscontainer.vpc is not None
    stack = Vpc(context.awscontainer.vpc).stack

    state = {"step": 0}
    sequence = ["UPDATE_IN_PROGRESS", "UPDATE_ROLLBACK_IN_PROGRESS"]
    monkeypatch.setattr(
        type(stack),
        "status_detail",
        property(lambda self: sequence[min(state["step"], len(sequence) - 1)]),
    )
    monkeypatch.setattr(
        "pocket_cli.resources.aws.cloudformation.time.sleep",
        lambda _: state.__setitem__("step", state["step"] + 1),
    )
    with pytest.raises(RuntimeError, match="rolled back"):
        stack.wait_status("COMPLETED", timeout=30, interval=1)


@mock_aws
def test_wait_status_accepts_stack_already_rolled_back_at_rest(use_toml, monkeypatch):
    """依存リソース待ちで rest 状態の UPDATE_ROLLBACK_COMPLETE を見た場合は
    安定状態 (COMPLETED) として成功すること"""
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.awscontainer is not None
    assert context.awscontainer.vpc is not None
    stack = Vpc(context.awscontainer.vpc).stack

    monkeypatch.setattr(
        type(stack),
        "status_detail",
        property(lambda self: "UPDATE_ROLLBACK_COMPLETE"),
    )
    # raise せず COMPLETED として返ること
    stack.wait_status("COMPLETED", timeout=5, interval=1)


def test_stack_description_none_only_for_not_exist(use_toml, monkeypatch):
    """description は「不存在」のみ None、throttling 等は伝播すること

    以前は except ClientError 全捕捉で throttling / AccessDenied も
    「スタック不存在」に潰れ、create_stack の AlreadyExists 衝突や
    wait_status の誤中断 (「Stack が見つかりません」) につながっていた。
    """
    from botocore.exceptions import ClientError

    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.awscontainer is not None
    assert context.awscontainer.vpc is not None
    stack = Vpc(context.awscontainer.vpc).stack

    def _raise(error_code, message):
        def _call(**kwargs):
            raise ClientError(
                {"Error": {"Code": error_code, "Message": message}}, "DescribeStacks"
            )

        return _call

    # 不存在 → None
    monkeypatch.setattr(
        stack.client,
        "describe_stacks",
        _raise("ValidationError", "Stack with id x does not exist"),
        raising=False,
    )
    assert stack.description is None

    # throttling → 伝播
    stack.__dict__.pop("description", None)
    monkeypatch.setattr(
        stack.client,
        "describe_stacks",
        _raise("Throttling", "Rate exceeded"),
        raising=False,
    )
    with pytest.raises(ClientError, match="Rate exceeded"):
        _ = stack.description
