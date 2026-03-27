"""Stack.output が OutputKey と ExportName の両方で引けることを検証する"""

from moto import mock_aws
from pocket_cli.resources.vpc import Vpc

from pocket.context import Context


@mock_aws
def test_output_by_output_key(use_toml):
    """OutputKey でスタック output を取得できる"""
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.awscontainer is not None
    assert context.awscontainer.vpc is not None

    vpc = Vpc(context.awscontainer.vpc)
    vpc.create()
    vpc.stack.clear_status()

    output = vpc.stack.output
    assert output is not None
    # OutputKey で取得できる
    assert "VPC" in output


@mock_aws
def test_output_by_export_name(use_toml):
    """ExportName でスタック output を取得できる"""
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.awscontainer is not None
    assert context.awscontainer.vpc is not None

    vpc = Vpc(context.awscontainer.vpc)
    vpc.create()
    vpc.stack.clear_status()

    output = vpc.stack.output
    export = vpc.stack.export
    assert output is not None

    # ExportName で取得できる
    vpc_id_export = export["vpc_id"]
    assert vpc_id_export in output

    # private_subnet_ の ExportName で取得できる
    subnet_prefix = export["private_subnet_"]
    assert f"{subnet_prefix}1" in output


@mock_aws
def test_rds_can_resolve_vpc_subnets(use_toml):
    """RDS が VPC スタックの ExportName 経由でサブネットを解決できる"""
    use_toml("tests/data/toml/rds.toml")
    context = Context.from_toml(stage="dev")
    assert context.rds is not None

    from pocket_cli.resources.rds import Rds

    vpc = Vpc(context.rds.vpc)
    vpc.create()

    rds = Rds(context.rds)
    subnet_ids = rds._get_vpc_subnet_ids()
    assert len(subnet_ids) >= 2  # zone_suffixes = ["a", "c"]

    vpc_id = rds._get_vpc_id()
    assert vpc_id.startswith("vpc-")
