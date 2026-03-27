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
