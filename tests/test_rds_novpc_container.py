"""use_vpc=false の container と [rds] の共存の回帰テスト (feedback KN1352)。

`LambdaRDSAccess` (RDS SG への ingress) が `{% if use_rds %}` だけで囲まれ、
`{% if vpc %}` 内でしか定義されない `LambdaSecurityGroup` を参照していたため、
[rds] のある stage に use_vpc=false の container を足すと CreateStack が
"Unresolved resource dependencies [LambdaSecurityGroup]" の validation で
落ちていた。VPC 外 Lambda は RDS SG への ingress 自体が不要なので、
use_vpc=false では LambdaRDSAccess を出力しない。

あわせて、この組合せは deploy 前に気付けるよう Settings.from_toml の
advisory で「RDS に到達できない」旨を警告する。
"""

from pocket.context import Context
from pocket.settings import Settings

_TOML = """\
[general]
region = "ap-northeast-1"
project_name = "testprj"
stages = ["dev"]

[vpc]
ref = "main"
zone_suffixes = ["a", "c"]

[rds]
managed = false
secret_arn = "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:x"
security_group_id = "sg-0123456789abcdef0"

[container.main]
dockerfile_path = "pocket.Dockerfile"

[container.main.handlers.wsgi]
command = "pocket.django.lambda_handlers.wsgi_handler"

[container.v2]
dockerfile_path = "v2/Dockerfile"
use_vpc = false

[container.v2.handlers.wsgi]
command = "app.handler"
"""


def _write_toml(tmp_path):
    p = tmp_path / "pocket.toml"
    p.write_text(_TOML)
    return str(p)


def test_novpc_container_yaml_has_no_rds_ingress(use_toml, tmp_path):
    from pocket_cli.resources.aws.cloudformation import ContainerStack

    use_toml(_write_toml(tmp_path))
    context = Context.from_toml(stage="dev")
    yaml = ContainerStack(context.container["v2"], rds_context=context.rds).yaml
    # LambdaSecurityGroup は vpc 無しでは定義されないため、参照が残っていると
    # CreateStack の validation に落ちる。定義・参照とも無いことを確認する
    # (テンプレート由来の説明コメントに資源名が残るため、定義 (name:) と
    # 参照 (Ref:) の形で判定する)
    assert "LambdaSecurityGroup:" not in yaml
    assert "Ref: LambdaSecurityGroup" not in yaml
    assert "LambdaRDSAccess:" not in yaml


def test_vpc_container_yaml_keeps_rds_ingress(use_toml, tmp_path):
    from pocket_cli.resources.aws.cloudformation import ContainerStack

    use_toml(_write_toml(tmp_path))
    context = Context.from_toml(stage="dev")
    yaml = ContainerStack(context.container["main"], rds_context=context.rds).yaml
    assert "LambdaRDSAccess" in yaml
    assert "LambdaSecurityGroup:" in yaml


def test_novpc_container_with_rds_warns(use_toml, tmp_path, capsys):
    use_toml(_write_toml(tmp_path))
    Settings.from_toml(stage="dev")
    err = capsys.readouterr().err
    assert "container.v2" in err
    assert "到達できません" in err
    assert "container.main" not in err
