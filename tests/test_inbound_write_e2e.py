"""inbound / inbound-domain の手順 (pocket-rs) を moto server 相手に確かめる。

部品の入口は非表示の `pocket-rs deploy-parts` (通しの deploy は moto で作れない
stack で止まるため、部品ごとに通す)。

moto は inbound / inbound-domain の stack を作れない (`AWS::SES::EmailIdentity` と
`AWS::CloudWatch::Alarm` に対応していない)。stack の作成・更新・削除は Rust 側の fake の
テストで見て、ここでは同じ名前・同じ output の stack と SES の rule をテスト側で置き、
検査の部品が AWS の API を正しく読むことを確かめる。Python 版の実装にも同じ状態を
判定させて、結果が一致することを見る。

moto で通せない範囲 (Rust 側の fake のテストで見る):

- 削除の配送猶予 (36 時間) の経過と queue の滞留: stack の更新時刻を動かせない
- 受信ドメインの検証待ち: moto の identity は作った時点で SUCCESS になる

SES の active な rule set は region ごとに 1 つなので、テストごとに別の region を使う。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import boto3

_PY_NAMES = """
import json, sys
from pocket.context import Context
ctx = Context.from_toml(stage=sys.argv[1])
print(json.dumps({
    "inbound": {name: inlet.resource_name for name, inlet in ctx.inbound.items()},
    "domain": {name: d.stack_name for name, d in ctx.inbound_domain.items()},
}))
"""

_PY_POST = """
import sys
from pocket.context import Context
from pocket_cli.resources.inbound import Inbound
ctx = Context.from_toml(stage=sys.argv[1])
for inlet in ctx.inbound.values():
    try:
        Inbound(inlet).ensure_post_deploy_state()
    except ValueError as error:
        print(error)
        sys.exit(0)
print("ok")
"""

_PY_PREPARE_DOMAIN = """
import sys
from pocket.context import Context
from pocket_cli.resources.inbound_domain import InboundDomain
ctx = Context.from_toml(stage=sys.argv[1])
for domain in ctx.inbound_domain.values():
    try:
        InboundDomain(domain).prepare_deploy()
    except ValueError as error:
        print(str(error).splitlines()[0])
        sys.exit(0)
print("ok")
"""

DOMAIN = "mail.example.com"
RULE_SET = "default-rule-set"


def _client(service: str, endpoint: str, region: str):
    return boto3.client(
        service,
        region_name=region,
        endpoint_url=endpoint,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106 moto 用のダミー
    )


def _toml(region: str, project: str, inbound: str = "") -> str:
    return (
        "[general]\n"
        f'region = "{region}"\n'
        f'project_name = "{project}"\n'
        'stages = ["prod"]\n\n'
        "[container.web]\n"
        'dockerfile_path = "Dockerfile"\n\n'
        "[container.web.handlers.mail]\n"
        'command = "handler.mail"\n'
        "sqs = { dead_letter_alert = { enabled = false } }\n\n" + inbound
    )


def _inbound(name: str = "support", extra: str = "") -> str:
    return (
        f"[inbound.{name}]\n"
        f'domain = "{DOMAIN}"\n'
        "manage_dns = false\n"
        f'recipients = ["{name}@{DOMAIN}"]\n'
        'handler = "web.mail"\n'
        "retention_days = 30\n"
        'delivery_alert = { email = "ops@example.com" }\n' + extra
    )


def _project(tmp_path: Path, toml: str) -> Path:
    root = tmp_path / "prj"
    root.mkdir(exist_ok=True)
    (root / "pocket.toml").write_text(toml)
    (root / "pyproject.toml").write_text('[project]\nname = "e2e-prj"\n')
    return root


def _env(pocket_rs, region: str) -> dict[str, str]:
    return {**pocket_rs.env, "AWS_DEFAULT_REGION": region}


def _part(pocket_rs, root: Path, region: str, part: str, *args: str):
    return subprocess.run(  # noqa: S603 shell=False + 制御された引数
        [str(pocket_rs.binary), "deploy-parts", part, "--stage", "prod", *args],
        cwd=root,
        env=_env(pocket_rs, region),
        capture_output=True,
        text=True,
    )


def _ok(pocket_rs, root: Path, region: str, part: str, *args: str):
    result = _part(pocket_rs, root, region, part, *args)
    assert result.returncode == 0, result.stderr + result.stdout
    return result


def _fails(pocket_rs, root: Path, region: str, part: str, *args: str) -> str:
    result = _part(pocket_rs, root, region, part, *args)
    assert result.returncode != 0, result.stdout
    return result.stderr


def _py(pocket_rs, root: Path, region: str, script: str) -> str:
    result = subprocess.run(  # noqa: S603 sys.executable + 固定スクリプト
        [sys.executable, "-c", script, "prod"],
        cwd=root,
        env=_env(pocket_rs, region),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


def _names(pocket_rs, root: Path, region: str) -> dict:
    return json.loads(_py(pocket_rs, root, region, _PY_NAMES))


def _activate_rule_set(endpoint: str, region: str):
    ses = _client("ses", endpoint, region)
    ses.create_receipt_rule_set(RuleSetName=RULE_SET)
    ses.set_active_receipt_rule_set(RuleSetName=RULE_SET)
    return ses


def _put_stack(
    endpoint: str,
    region: str,
    name: str,
    outputs: dict[str, str],
    resources: dict | None = None,
):
    """`name` の stack を、決めた output で置く (既定の中身は SNS の topic 1 つ)。"""
    marker = {
        "Marker": {
            "Type": "AWS::SNS::Topic",
            "Properties": {"TopicName": f"{name}-marker"},
        }
    }
    template = {
        "Resources": resources or marker,
        "Outputs": {key: {"Value": value} for key, value in outputs.items()},
    }
    cfn = _client("cloudformation", endpoint, region)
    cfn.create_stack(StackName=name, TemplateBody=json.dumps(template))
    return cfn


def _deployed_inbound(endpoint: str, region: str, stack: str, *, enabled: bool):
    """宣言どおりに deploy した状態を置く: bucket・topic・queue・SES の rule・stack。

    stack の template は、deploy 前の検査が読む部分 (bucket の名前と rule) だけを
    本物と同じ形にする。moto は `AWS::SES::ReceiptRule` を作らないので、
    rule は API で置く。
    """
    bucket = f"{stack}-123456789012"
    topic = _client("sns", endpoint, region).create_topic(Name=stack)["TopicArn"]
    sqs = _client("sqs", endpoint, region)
    queues = {
        key: sqs.create_queue(QueueName=f"{stack}-{key}")["QueueUrl"]
        for key in ("QueueUrl", "WorkerDLQUrl", "DeliveryDLQUrl")
    }
    rule = {
        "Name": stack,
        "Enabled": enabled,
        "TlsPolicy": "Require",
        "ScanEnabled": True,
        "Recipients": [f"support@{DOMAIN}"],
        "Actions": [
            {
                "S3Action": {
                    "BucketName": bucket,
                    "ObjectKeyPrefix": "raw/",
                    "TopicArn": topic,
                }
            }
        ],
    }
    outputs = {
        "RuleSet": RULE_SET,
        "RuleName": stack,
        "Bucket": bucket,
        "TopicArn": topic,
        "Enabled": "true" if enabled else "false",
        "Handler": "web.mail",
        "RawPrefix": "raw/",
        "MetadataPrefix": "metadata/",
        "ImportPrefix": "imports/",
        **queues,
    }
    resources = {
        "Bucket": {
            "Type": "AWS::S3::Bucket",
            "Properties": {"BucketName": {"Fn::Sub": f"{stack}-${{AWS::AccountId}}"}},
        },
        "ReceiptRule": {
            "Type": "AWS::SES::ReceiptRule",
            "Properties": {
                "RuleSetName": RULE_SET,
                "Rule": {
                    "Name": stack,
                    "Enabled": enabled,
                    "Recipients": rule["Recipients"],
                },
            },
        },
    }
    _put_stack(endpoint, region, stack, outputs, resources)
    _client("ses", endpoint, region).create_receipt_rule(
        RuleSetName=RULE_SET, Rule=rule
    )
    return rule


def test_inbound_prepare_resolves_rule_set(pocket_rs, moto_endpoint, tmp_path):
    region = "us-east-2"
    root = _project(tmp_path, _toml(region, "inbprep", _inbound()))
    # active な rule set が無ければ、作り方を案内して止まる (pocket は作らない)
    error = _fails(pocket_rs, root, region, "inbound-prepare")
    assert "active な receipt rule set がありません" in error
    assert f"--rule-set-name {RULE_SET} --region {region}" in error

    ses = _activate_rule_set(moto_endpoint, region)
    _ok(pocket_rs, root, region, "inbound-prepare")
    assert ses.describe_active_receipt_rule_set()["Rules"] == []

    # 同じ宛先を受ける既存の rule があれば止まる
    _client("s3", moto_endpoint, region).create_bucket(
        Bucket="other-inbound-bucket",
        CreateBucketConfiguration={"LocationConstraint": region},
    )
    ses.create_receipt_rule(
        RuleSetName=RULE_SET,
        Rule={
            "Name": "other",
            "Enabled": True,
            "Recipients": [DOMAIN],
            "Actions": [{"S3Action": {"BucketName": "other-inbound-bucket"}}],
        },
    )
    error = _fails(pocket_rs, root, region, "inbound-prepare")
    assert "既存ルール other と競合します" in error


def test_inbound_post_agrees_with_python(pocket_rs, moto_endpoint, tmp_path):
    region = "us-west-1"
    root = _project(tmp_path, _toml(region, "inbpost", _inbound()))
    stack = _names(pocket_rs, root, region)["inbound"]["support"]
    ses = _activate_rule_set(moto_endpoint, region)
    # stack が無いうちは確認できない
    error = _fails(pocket_rs, root, region, "inbound-post")
    assert "受信スタックの出力を確認できません" in error

    rule = _deployed_inbound(moto_endpoint, region, stack, enabled=True)
    _ok(pocket_rs, root, region, "inbound-post")
    assert _py(pocket_rs, root, region, _PY_POST) == "ok"
    # deploy 済みの受信口は、次の deploy の前の検査も通る
    _ok(pocket_rs, root, region, "inbound-prepare")

    # stack の外で rule を書き換えると、Rust も Python も不一致で止まる
    message = "SES受信ルールがdeployした宣言と一致しません"
    for change in (
        {"Enabled": False},
        {"Recipients": [f"support@{DOMAIN}", f"extra@{DOMAIN}"]},
        {"ScanEnabled": False},
    ):
        ses.update_receipt_rule(RuleSetName=RULE_SET, Rule={**rule, **change})
        assert message in _fails(pocket_rs, root, region, "inbound-post")
        assert _py(pocket_rs, root, region, _PY_POST) == message
    ses.update_receipt_rule(RuleSetName=RULE_SET, Rule=rule)
    _ok(pocket_rs, root, region, "inbound-post")


def test_inbound_prepare_rejects_pinned_setting_change(
    pocket_rs, moto_endpoint, tmp_path
):
    region = "us-west-2"
    root = _project(tmp_path, _toml(region, "inbpin", _inbound()))
    stack = _names(pocket_rs, root, region)["inbound"]["support"]
    _activate_rule_set(moto_endpoint, region)
    _deployed_inbound(moto_endpoint, region, stack, enabled=True)
    _ok(pocket_rs, root, region, "inbound-prepare")
    # 受信済みの object の置き場所は、deploy 済みの受信口では変えられない
    changed = _inbound(extra='raw_prefix = "raw2/"\n')
    (root / "pocket.toml").write_text(_toml(region, "inbpin", changed))
    error = _fails(pocket_rs, root, region, "inbound-prepare")
    assert "RawPrefixの変更は新しいinbound名で移行してください" in error
    # 受信が有効なまま宛先を変えない (deploy 済みの template の rule と比べる)
    changed = _inbound().replace(f"support@{DOMAIN}", f"other@{DOMAIN}")
    (root / "pocket.toml").write_text(_toml(region, "inbpin", changed))
    error = _fails(pocket_rs, root, region, "inbound-prepare")
    assert "宛先変更前に enabled = false でdeployし" in error


def test_inbound_delete_guards(pocket_rs, moto_endpoint, tmp_path):
    region = "eu-west-1"
    root = _project(tmp_path, _toml(region, "inbdel", _inbound()))
    stack = _names(pocket_rs, root, region)["inbound"]["support"]
    _activate_rule_set(moto_endpoint, region)
    cfn = _client("cloudformation", moto_endpoint, region)
    # stack が無ければ何もしない
    _ok(pocket_rs, root, region, "inbound-delete", "--name", "support")

    # 受信が有効なままでは消さない
    rule = _deployed_inbound(moto_endpoint, region, stack, enabled=True)
    error = _fails(pocket_rs, root, region, "inbound-delete", "--name", "support")
    assert "先に inbound.enabled = false でdeployし" in error

    # rule だけ止めても、stack の output が止まっていなければ消さない
    ses = _client("ses", moto_endpoint, region)
    ses.update_receipt_rule(RuleSetName=RULE_SET, Rule={**rule, "Enabled": False})
    error = _fails(pocket_rs, root, region, "inbound-delete", "--name", "support")
    assert "先に inbound.enabled = false でdeployし" in error
    assert cfn.describe_stacks(StackName=stack)["Stacks"]


def test_inbound_delete_requires_grace_period(pocket_rs, moto_endpoint, tmp_path):
    region = "eu-west-2"
    stopped = _inbound(extra="enabled = false\n")
    root = _project(tmp_path, _toml(region, "inbgrace", stopped))
    stack = _names(pocket_rs, root, region)["inbound"]["support"]
    _activate_rule_set(moto_endpoint, region)
    _deployed_inbound(moto_endpoint, region, stack, enabled=False)
    _ok(pocket_rs, root, region, "inbound-post")
    # 受信を止めた直後 (stack を作った直後) は、配送猶予が足りない
    error = _fails(pocket_rs, root, region, "inbound-delete", "--name", "support")
    assert "最後のスタック更新から36時間の配送猶予が必要です" in error
    cfn = _client("cloudformation", moto_endpoint, region)
    assert cfn.describe_stacks(StackName=stack)["Stacks"]


def test_inbound_check_removed(pocket_rs, moto_endpoint, tmp_path):
    region = "eu-central-1"
    root = _project(tmp_path, _toml(region, "inbgone", _inbound()))
    stack = _names(pocket_rs, root, region)["inbound"]["support"]
    cfn = _put_stack(moto_endpoint, region, stack, {"RuleSet": RULE_SET})
    cfn = _put_stack(moto_endpoint, region, "old-inbound-stack", {"RuleSet": RULE_SET})
    state = tmp_path / "state.json"
    saved = {
        "support": {"stack_name": stack},
        "old": {"stack_name": "old-inbound-stack"},
        "never": {"stack_name": "never-created-stack"},
    }
    state.write_text(json.dumps({"resources": {"inbound": saved}}))
    args = ("inbound-check-removed", "--state", str(state))
    # 宣言から消した受信口の stack が残っていれば止まる
    error = _fails(pocket_rs, root, region, *args)
    assert "inbound.oldがAWS上に残っています" in error
    cfn.delete_stack(StackName="old-inbound-stack")
    _ok(pocket_rs, root, region, *args)
    # 記録が無い state (初回の deploy)
    state.write_text(json.dumps({"resources": {}}))
    _ok(pocket_rs, root, region, *args)


def test_inbound_domain_prepare_rejects_existing_identity(
    pocket_rs, moto_endpoint, tmp_path
):
    region = "ap-northeast-1"
    root = _project(tmp_path, _toml(region, "inbdom", _inbound()))
    stack = _names(pocket_rs, root, region)["domain"][DOMAIN]
    _ok(pocket_rs, root, region, "inbound-domain-prepare")
    assert _py(pocket_rs, root, region, _PY_PREPARE_DOMAIN) == "ok"

    # pocket が作っていない identity は取り込まない
    sesv2 = _client("sesv2", moto_endpoint, region)
    sesv2.create_email_identity(EmailIdentity=DOMAIN)
    error = _fails(pocket_rs, root, region, "inbound-domain-prepare")
    first_line = f"{DOMAIN} のSES identityが既に存在します。"
    assert first_line in error
    assert (
        f"aws sesv2 delete-email-identity --email-identity {DOMAIN} --region {region}"
        in error
    )
    assert _py(pocket_rs, root, region, _PY_PREPARE_DOMAIN).startswith(first_line)

    # stack が在れば、identity は pocket のもの
    _put_stack(moto_endpoint, region, stack, {"MxValue": "10 inbound-smtp"})
    _ok(pocket_rs, root, region, "inbound-domain-prepare")
    assert _py(pocket_rs, root, region, _PY_PREPARE_DOMAIN) == "ok"


def test_inbound_domain_confirm_and_delete(pocket_rs, moto_endpoint, tmp_path):
    region = "ap-southeast-2"
    root = _project(tmp_path, _toml(region, "inbconf", _inbound()))
    stack = _names(pocket_rs, root, region)["domain"][DOMAIN]
    outputs = {"MxValue": f"10 inbound-smtp.{region}.amazonaws.com"}
    for i in (1, 2, 3):
        outputs[f"DkimName{i}"] = f"token{i}._domainkey.{DOMAIN}"
        outputs[f"DkimValue{i}"] = f"token{i}.dkim.amazonses.com"
    cfn = _put_stack(moto_endpoint, region, stack, outputs)

    # 外部 DNS で未検証 (identity が無い): 待たずに、登録すべき record を示す
    result = _ok(pocket_rs, root, region, "inbound-domain-confirm")
    assert f"inbound domain {DOMAIN}: verification=None" in result.stderr
    assert result.stdout.splitlines() == [
        f"  CNAME token1._domainkey.{DOMAIN} token1.dkim.amazonses.com",
        f"  CNAME token2._domainkey.{DOMAIN} token2.dkim.amazonses.com",
        f"  CNAME token3._domainkey.{DOMAIN} token3.dkim.amazonses.com",
        f"  MX {DOMAIN} 10 inbound-smtp.{region}.amazonaws.com",
    ]

    # 検証済み (moto の identity は作った時点で SUCCESS)
    sesv2 = _client("sesv2", moto_endpoint, region)
    sesv2.create_email_identity(EmailIdentity=DOMAIN)
    result = _ok(pocket_rs, root, region, "inbound-domain-confirm")
    assert (
        f"inbound domain {DOMAIN}: verification=SUCCESS (manage_dns = false)"
        in result.stderr
    )
    assert result.stdout == ""

    _ok(pocket_rs, root, region, "inbound-domain-delete")
    assert stack not in [s["StackName"] for s in cfn.describe_stacks()["Stacks"]]
    # 無い stack の削除は何もしない
    _ok(pocket_rs, root, region, "inbound-domain-delete")


def test_inbound_domain_cleanup(pocket_rs, moto_endpoint, tmp_path):
    region = "ca-central-1"
    root = _project(tmp_path, _toml(region, "inbclean", _inbound()))
    stack = _names(pocket_rs, root, region)["domain"][DOMAIN]
    cfn = _put_stack(moto_endpoint, region, stack, {"MxValue": "x"})
    _put_stack(moto_endpoint, region, "old-domain-stack", {"MxValue": "x"})
    state = tmp_path / "state.json"
    saved = {
        DOMAIN: {"stack_name": stack},
        "old.example.com": {"stack_name": "old-domain-stack"},
        "gone.example.com": {"stack_name": "gone-domain-stack"},
    }
    state.write_text(json.dumps({"resources": {"inbound_domain": saved}}))
    result = _ok(
        pocket_rs, root, region, "inbound-domain-cleanup", "--state", str(state)
    )
    assert "Deleting unused inbound domain old.example.com..." in result.stderr
    assert "gone.example.com" not in result.stderr
    # 宣言に残るドメインの stack は消さない
    names = [s["StackName"] for s in cfn.describe_stacks()["Stacks"]]
    assert stack in names
    assert "old-domain-stack" not in names
