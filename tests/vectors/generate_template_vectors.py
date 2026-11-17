"""CloudFormation template の共有 test vector を生成する (`just vectors`)。

Python 版の deploy が作る stack の template を、入力 (toml × その stages) ごとに
`tests/data/template_vectors/<group>/<名前>.<stage>/` へ書く。Rust 版 (`pocket-rs`) は
同じ入力から template を組み、この出力とバイト一致することを確かめる
(stack の `pocket:template_hash` タグは template のテキストの hash なので、
1 バイトでも違うと既存 stack が REQUIRE_UPDATE になる)。

出力:

- `manifest.json`: 入力・stage・外部入力と、stack の一覧 (種別・stack 名・region・
  capabilities・tags・template hash・ファイル名)
- `<種別>.<stack 名>.yaml` (inbound 系は `.json`): template のテキスト + 改行 1 つ
- `<種別>.<stack 名>.input.json`: Jinja2 の template に渡した変数 (inbound 系は
  template を Python の dict で組むので無い)。dict の順は描画の順なので並べ替えない

外部入力 (他 stack の output・secret の値・RDS / DSQL / EFS の実体・receipt rule set
の binding) は固定の fake に差し替え、使った値を manifest の `stack_external` に
同梱する (Rust は同じ値を注入する)。Context の外部入力は settings の vector と同じ
fake (`generate_settings_vectors.fake_external`) で、SM の managed secret は
全 stage で「作成済み」にする (未作成だと IAM policy の Resource を組めない)。

生成器は「JSON にした変数で描画し直しても同じテキストになる」ことも確かめる
(Rust が受け取るのは JSON の値だけなので、Python のオブジェクトに依存した描画が
あると Rust で再現できない)。

`--check` は書かずに、commit 済みの vector と一致するか (不足・余分を含む) を見る。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import click
from generate_settings_vectors import (
    REPO_ROOT,
    _place_managed_assets,
    fake_external,
    input_context,
    isolated_load,
    iter_inputs,
)
from pocket_cli.resources.aws.cloudformation import (
    AcmStack,
    CloudFrontKeysStack,
    CloudFrontStack,
    CloudFrontWafStack,
    ContainerStack,
    Stack,
    VpcStack,
    render_template,
)
from pocket_cli.resources.aws.efs import Efs
from pocket_cli.resources.dsql import Dsql
from pocket_cli.resources.inbound import InboundStack
from pocket_cli.resources.inbound_domain import InboundDomainStack
from pocket_cli.resources.rds import Rds
from pydantic import ValidationError
from pydantic_core import to_jsonable_python

from pocket.context import ORIGIN_VERIFY_SECRET_KEY, Context
from pocket.settings import Settings
from pocket.utils import tomllib

OUT_DIR = REPO_ROOT / "tests/data/template_vectors"

FAKE_ACCOUNT = "123456789012"
FAKE_EFS_FILESYSTEM_ID = "fs-0123456789abcdef0"
FAKE_RDS_SECURITY_GROUP_ID = "sg-0123456789abcdef0"
FAKE_DSQL_IDENTIFIER = "vectordsqlclusteridentifier"
FAKE_SIGNING_PUBLIC_KEY_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAvector\n"
    "-----END PUBLIC KEY-----\n"
)
FAKE_RULE_SET = "default-rule-set"
FAKE_PREVIOUS_RULE = "vector-previous-rule"
# 外部 VPC (manage = false) の private subnet の数
FAKE_EXTERNAL_VPC_ZONES = 2


def fake_stack_output(stack: Stack) -> dict[str, str] | None:
    """他 stack の output の fake (参照されうるものだけ)"""
    if isinstance(stack, AcmStack):
        return {
            "CertificateArn": "arn:aws:acm:us-east-1:%s:certificate/%s"
            % (FAKE_ACCOUNT, stack.name)
        }
    if isinstance(stack, CloudFrontWafStack):
        return {
            "WebACLArn": "arn:aws:wafv2:us-east-1:%s:global/webacl/%s/vector"
            % (FAKE_ACCOUNT, stack.name)
        }
    if isinstance(stack, VpcStack):
        prefix = stack.export["private_subnet_"]
        return {
            "%s%d" % (prefix, i): "subnet-vector%d" % i
            for i in range(1, FAKE_EXTERNAL_VPC_ZONES + 1)
        }
    # inbound は未 deploy (新規の binding) として扱う
    return None


def fake_rds_cluster(rds: Rds) -> dict[str, Any]:
    cluster: dict[str, Any] = {
        "Endpoint": "%s.cluster-vector.%s.rds.amazonaws.com"
        % (rds.context.cluster_identifier, rds.context.region),
        "Port": 5432,
    }
    if rds.context.password_strategy != "static":  # noqa: S105 戦略名であって secret 値ではない
        cluster["MasterUserSecret"] = {
            "SecretArn": "arn:aws:secretsmanager:%s:%s:secret:rds!cluster-vector"
            % (rds.context.region, FAKE_ACCOUNT)
        }
    return cluster


def fake_rds_static_secret(rds: Rds) -> dict[str, str]:
    return {
        "ARN": "arn:aws:secretsmanager:%s:%s:secret:%s-AbCdEf"
        % (rds.context.region, FAKE_ACCOUNT, rds.context.credentials_secret_name)
    }


@contextlib.contextmanager
def fake_stack_external() -> Iterator[dict]:
    """stack の描画が引く外部入力を fake にし、引いた値を記録する"""
    used: dict[str, Any] = {
        "stack_outputs": {},
        "rds": None,
        "dsql_identifier": None,
        "efs_filesystem_id": None,
        "inbound_bindings": {},
    }

    def output(self: Stack) -> dict[str, str] | None:
        value = fake_stack_output(self)
        used["stack_outputs"][self.name] = value
        return value

    def rds_cluster(self: Rds) -> dict[str, Any]:
        cluster = fake_rds_cluster(self)
        used["rds"] = {**(used["rds"] or {}), "cluster": cluster}
        return cluster

    def rds_security_group(self: Rds) -> dict[str, str]:
        used["rds"] = {
            **(used["rds"] or {}),
            "security_group_id": FAKE_RDS_SECURITY_GROUP_ID,
        }
        return {"GroupId": FAKE_RDS_SECURITY_GROUP_ID}

    def rds_static_secret(self: Rds) -> dict[str, str]:
        secret = fake_rds_static_secret(self)
        used["rds"] = {**(used["rds"] or {}), "static_secret_arn": secret["ARN"]}
        return secret

    def dsql_cluster(self: Dsql) -> dict[str, str]:
        used["dsql_identifier"] = FAKE_DSQL_IDENTIFIER
        return {"identifier": FAKE_DSQL_IDENTIFIER}

    def efs_description(self: Efs) -> dict[str, str]:
        used["efs_filesystem_id"] = FAKE_EFS_FILESYSTEM_ID
        return {"FileSystemId": FAKE_EFS_FILESYSTEM_ID}

    def inbound_binding(self: InboundStack) -> tuple[str, str | None]:
        # 1 件目は rule set の先頭、2 件目以降は既存 rule の後ろ (両方の形を通す)
        after = FAKE_PREVIOUS_RULE if used["inbound_bindings"] else None
        rule_set = self.context.config.rule_set or FAKE_RULE_SET
        used["inbound_bindings"][self.name] = {"rule_set": rule_set, "after": after}
        return rule_set, after

    with (
        mock.patch.object(Stack, "output", property(output)),
        mock.patch.object(Rds, "cluster", property(rds_cluster)),
        mock.patch.object(Rds, "_security_group", property(rds_security_group)),
        mock.patch.object(Rds, "_static_secret", property(rds_static_secret)),
        mock.patch.object(Dsql, "cluster", property(dsql_cluster)),
        mock.patch.object(Efs, "description", property(efs_description)),
        mock.patch.object(InboundStack, "binding", inbound_binding),
    ):
        yield used


def _edge_stacks(context: Context, secrets: dict) -> list[tuple[str, Stack]]:
    """us-east-1 に先に作る stack (ACM 証明書 → WAF)"""
    stacks: list[tuple[str, Stack]] = []
    for cf in context.cloudfront.values():
        if cf.domain:
            stacks.append(("acm", AcmStack(cf)))
    for cf in context.cloudfront.values():
        if cf.waf is None:
            continue
        values = {
            key: "vector-allow-%s" % key.lower() for key in cf.waf.header_secret_keys
        }
        secrets["waf_allow_secret_values"][cf.name] = values
        stacks.append(("waf", CloudFrontWafStack(cf, allow_secret_values=values)))
    return stacks


def _vpc_stacks(context: Context) -> list[tuple[str, Stack]]:
    stacks: list[tuple[str, Stack]] = []
    names: set[str] = set()
    for container in context.container.values():
        vpc = container.vpc
        if vpc and vpc.manage and vpc.name not in names:
            names.add(vpc.name)
            stacks.append(("vpc", VpcStack(vpc)))
    return stacks


def _keys_stacks(context: Context, secrets: dict) -> list[tuple[str, Stack]]:
    stacks: list[tuple[str, Stack]] = []
    for cf in context.cloudfront.values():
        if not cf.signing_key:
            continue
        secrets["signing_public_key_pem"][cf.name] = FAKE_SIGNING_PUBLIC_KEY_PEM
        stack = CloudFrontKeysStack(
            cf, signing_public_key_pem=FAKE_SIGNING_PUBLIC_KEY_PEM
        )
        stacks.append(("cloudfront_keys", stack))
    return stacks


def _cloudfront_stacks(context: Context, secrets: dict) -> list[tuple[str, Stack]]:
    stacks: list[tuple[str, Stack]] = []
    for cf in context.cloudfront.values():
        origin_verify = ""
        if cf.enable_origin_verify:
            origin_verify = "vector-%s" % ORIGIN_VERIFY_SECRET_KEY.lower()
            secrets["origin_verify_secret"][cf.name] = origin_verify
        stack = CloudFrontStack(cf, origin_verify_secret_value=origin_verify)
        stacks.append(("cloudfront", stack))
    return stacks


def build_stacks(context: Context, used: dict) -> list[tuple[str, Stack]]:
    """deploy が作る stack を deploy の順 (`get_resources`) で並べる"""
    secrets: dict[str, dict] = {
        "waf_allow_secret_values": {},
        "signing_public_key_pem": {},
        "origin_verify_secret": {},
    }
    used["secrets"] = secrets
    stacks = _edge_stacks(context, secrets)
    stacks.extend(
        ("inbound_domain", InboundDomainStack(ctx))
        for ctx in context.inbound_domain.values()
    )
    stacks.extend(_vpc_stacks(context))
    stacks.extend(_keys_stacks(context, secrets))
    for name in sorted(context.container):
        stack = ContainerStack(
            context.container[name],
            rds_context=context.rds,
            dsql_context=context.dsql,
            scheduler_context=context.scheduler.get(name),
        )
        stacks.append(("container", stack))
    stacks.extend(("inbound", InboundStack(ctx)) for ctx in context.inbound.values())
    stacks.extend(_cloudfront_stacks(context, secrets))
    return stacks


def render_stack(kind: str, stack: Stack) -> tuple[str, dict | None]:
    """(template のテキスト, Jinja2 に渡した変数 or None)"""
    captured: list[dict] = []
    original = Stack._render_input

    def recording(self: Stack, context_data: dict | None = None, **extra) -> dict:
        variables = original(self, context_data, **extra)
        captured.append(variables)
        return variables

    with mock.patch.object(Stack, "_render_input", recording):
        text = stack.yaml
    if not captured:
        return text, None
    variables = to_jsonable_python(captured[-1])
    again = render_template(stack.template_filename, variables)
    if again != text:
        raise click.ClickException(
            "%s (%s): JSON にした変数で描画すると結果が変わります "
            "(template が Python のオブジェクトに依存しています)" % (stack.name, kind)
        )
    return text, variables


def build_files(toml_path: Path, stage: str, context: dict) -> dict[str, str] | None:
    """1 入力 × 1 stage のファイル (名前 → 内容)。Context を組めない入力は None"""
    data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    files: dict[str, str] = {}
    entries = []
    with isolated_load(toml_path, context):
        dir_files = _place_managed_assets(data)
        with fake_external(True) as external, fake_stack_external() as used:
            settings = Settings.from_toml(stage=stage)
            try:
                pocket_context = Context.from_settings(settings)
            except (ValueError, RuntimeError, ValidationError):
                return None
            for kind, stack in build_stacks(pocket_context, used):
                text, variables = render_stack(kind, stack)
                ext = "yaml" if variables is not None else "json"
                base = "%s.%s" % (kind, stack.name)
                files["%s.%s" % (base, ext)] = text + "\n"
                entry = {
                    "kind": kind,
                    "name": stack.name,
                    "region": stack.client.meta.region_name,
                    "capabilities": stack.capabilities,
                    "tags": stack.stack_tags,
                    "export": stack.export,
                    "template_hash": hashlib.sha256(text.encode()).hexdigest()[:16],
                    "file": "%s.%s" % (base, ext),
                }
                if variables is not None:
                    entry["input_file"] = "%s.input.json" % base
                    files[entry["input_file"]] = (
                        json.dumps(variables, ensure_ascii=False, indent=2) + "\n"
                    )
                entries.append(entry)
        external = {**external, "dir_files": dir_files}
    manifest = {
        "input": toml_path.relative_to(REPO_ROOT).as_posix(),
        "stage": stage,
        "context": context,
        "external": external,
        "stack_external": used,
        "stacks": entries,
    }
    files["manifest.json"] = (
        json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    )
    return files


def expected_files() -> dict[Path, str]:
    files: dict[Path, str] = {}
    for group, name, path in iter_inputs():
        text = path.read_text(encoding="utf-8")
        context = input_context(text)
        for stage in tomllib.loads(text)["general"]["stages"]:
            built = build_files(path, stage, context)
            if built is None:
                continue
            directory = OUT_DIR / group / ("%s.%s" % (name, stage))
            for filename, content in built.items():
                files[directory / filename] = content
    return files


def existing_files() -> set[Path]:
    return {p for p in OUT_DIR.rglob("*") if p.is_file()}


def diff_files(expected: dict[Path, str]) -> list[str]:
    """commit 済みの vector との差 (相対 path と理由)"""
    problems = []
    for path, text in sorted(expected.items()):
        rel = path.relative_to(REPO_ROOT)
        if not path.is_file():
            problems.append("%s: ありません" % rel)
        elif path.read_text(encoding="utf-8") != text:
            problems.append("%s: 内容が違います" % rel)
    for path in sorted(existing_files() - set(expected)):
        problems.append("%s: 入力がありません (余分)" % path.relative_to(REPO_ROOT))
    return problems


@click.command()
@click.option("--check", is_flag=True, help="書かずに commit 済みの vector と比べる")
def main(check: bool):
    expected = expected_files()
    if check:
        problems = diff_files(expected)
        if problems:
            raise click.ClickException(
                "template vector が古いです (`just vectors` で再生成):\n  "
                + "\n  ".join(problems)
            )
        return
    for path in existing_files() - set(expected):
        path.unlink()
    for directory in sorted(OUT_DIR.rglob("*"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    for path, text in expected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    click.echo("%d 件の template vector を書きました" % len(expected), err=True)


if __name__ == "__main__":
    main()
