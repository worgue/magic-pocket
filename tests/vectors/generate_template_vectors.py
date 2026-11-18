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
- `roles.json`: stage で pocket が使う IAM role の一覧 (`stage_role_specs`。deploy 前の
  external_roles の検査と同じ並び)。inline policy は定義のままと、`Fn::Sub` を展開した
  IAM に直接書ける形の両方を持つ。dict の順は policy の定義順なので並べ替えない
- `documents.json`: template を通さず API に直接書く文書 (s3 か cloudfront がある stage
  だけ)。S3 bucket の望む設定と、cloudfront ごとの bucket policy の statement・
  policy の遷移 (今の policy → 確保した後 / 外した後)。RDS / EFS / DSQL は作成 API に
  渡す引数、backup は plan・selection の文書と「plan が宣言どおりか」の判定

外部入力 (他 stack の output・secret の値・RDS / DSQL / EFS の実体・receipt rule set
の binding・受信ドメインの hosted zone id) は固定の fake に差し替え、使った値を
manifest の `stack_external` に同梱する (Rust は同じ値を注入する)。Context の
外部入力は settings の vector と同じ fake (`generate_settings_vectors.fake_external`)
で、SM の managed secret は全 stage で「作成済み」にする (未作成だと IAM policy の
Resource を組めない)。

生成器は「JSON にした変数で描画し直しても同じテキストになる」ことも確かめる
(Rust が受け取るのは JSON の値だけなので、Python のオブジェクトに依存した描画が
あると Rust で再現できない)。

`--check` は書かずに、commit 済みの vector と一致するか (不足・余分を含む) を見る。
"""

from __future__ import annotations

import contextlib
import copy
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
from pocket_cli.resources import rds as rds_module
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
from pocket_cli.resources.aws.iam_roles import RoleSpec
from pocket_cli.resources.aws.stage_roles import stage_role_specs
from pocket_cli.resources.backup import Backup, _plan_matches
from pocket_cli.resources.cloudfront import CloudFront
from pocket_cli.resources.dsql import Dsql
from pocket_cli.resources.inbound import InboundStack
from pocket_cli.resources.inbound_domain import InboundDomainStack
from pocket_cli.resources.rds import Rds
from pocket_cli.resources.s3 import S3
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
FAKE_DISTRIBUTION_ID = "EVECTORDIST0001"
FAKE_VPC_ID = "vpc-0123456789abcdef0"
FAKE_SUBNET_IDS = ["subnet-0aaaaaaaaaaaaaaaa", "subnet-0bbbbbbbbbbbbbbbb"]
FAKE_MASTER_PASSWORD = "vector-master-password"  # noqa: S105 vector 用の固定値
FAKE_RDS_ENDPOINT = "vector.cluster-abc.ap-northeast-1.rds.amazonaws.com"
FAKE_SNAPSHOT = "vector-snapshot"
FAKE_BACKUP_ROLE_ARN = "arn:aws:iam::%s:role/vector-backup-role" % FAKE_ACCOUNT
FAKE_BACKUP_TARGET_ARN = "arn:aws:dsql:ap-northeast-1:%s:cluster/vector" % FAKE_ACCOUNT
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
        "inbound_domain_hosted_zone_ids": {},
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
    for ctx in context.inbound_domain.values():
        domain_stack = InboundDomainStack(ctx)
        # manage_dns = false なら None (レコードを作らず Outputs で案内する)
        used["inbound_domain_hosted_zone_ids"][domain_stack.name] = (
            domain_stack.hosted_zone_id
        )
        stacks.append(("inbound_domain", domain_stack))
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


def role_entry(spec: RoleSpec, context: Context) -> dict[str, Any]:
    """RoleSpec の全 field と、導出する文書 (信頼ポリシー・展開後の inline policy)。"""
    assert context.general is not None
    return {
        "name": spec.name,
        "service": spec.service,
        "managed_policy_arns": spec.managed_policy_arns,
        "inline_policies": spec.inline_policies,
        "permissions_boundary": spec.permissions_boundary,
        "assume_role_policy_version": spec.assume_role_policy_version,
        "external": spec.external,
        "assume_role_policy": spec.assume_role_policy,
        "resolved_inline_policies": spec.resolved_inline_policies(
            region=context.general.region, account_id=FAKE_ACCOUNT
        ),
    }


def s3_documents(context: Context) -> dict[str, Any]:
    """S3 bucket の望む設定 (`S3` が put する値。無いものは None)"""
    assert context.s3 is not None
    s3 = S3(context.s3, cloudfront_contexts=context.cloudfront)
    return {
        "public_access_block": s3.public_access_block_should_be,
        "cors_rules": s3._desired_cors_rules(),
        "lifecycle_rules": s3._desired_lifecycle_rules(),
        "versioning": context.s3.versioning,
    }


def _policy_cases(own: dict[str, Any]) -> list[tuple[str, dict | None]]:
    """bucket policy の遷移の入力 (名前, 今の policy)"""
    other = {
        "Sid": "VectorOther",
        "Effect": "Allow",
        "Principal": {"AWS": "arn:aws:iam::%s:root" % FAKE_ACCOUNT},
        "Action": "s3:ListBucket",
        "Resource": "arn:aws:s3:::vector-other",
    }
    same_sid = {**own, "Resource": "arn:aws:s3:::vector-other/*"}

    def policy(*statements: dict, version: str = "2012-10-17") -> dict:
        return {"Version": version, "Statement": list(statements)}

    return [
        ("none", None),
        ("own_only", policy(own)),
        ("other_only", policy(other)),
        ("other_and_own", policy(other, own)),
        ("same_sid_different_resource", policy(same_sid)),
        ("old_version_with_own", policy(own, version="2008-10-17")),
        ("old_version_without_own", policy(other, version="2008-10-17")),
    ]


def _policy_transition(cf_context, current: dict | None, method: str) -> dict[str, Any]:
    """`_ensure_bucket_policy` / `_delete_bucket_policy` が行う操作を記録する"""
    cloudfront = CloudFront.__new__(CloudFront)
    cloudfront.context = cf_context
    cloudfront.__dict__["account_id"] = FAKE_ACCOUNT
    cloudfront.__dict__["distribution_id"] = FAKE_DISTRIBUTION_ID
    cloudfront.__dict__["bucket_policy"] = copy.deepcopy(current)
    calls: list[dict[str, Any]] = []

    def record(policy: dict | None):
        if policy is None:
            calls.append({"action": "delete"})
        else:
            calls.append({"action": "put", "policy": copy.deepcopy(policy)})

    with mock.patch.object(cloudfront, "_update_origin_bucket_policy", record):
        getattr(cloudfront, method)()
    if len(calls) > 1:
        raise click.ClickException("%s が policy を複数回書きました" % method)
    return calls[0] if calls else {"action": "none"}


def cloudfront_documents(cf_context) -> dict[str, Any]:
    cloudfront = CloudFront.__new__(CloudFront)
    cloudfront.context = cf_context
    cloudfront.__dict__["account_id"] = FAKE_ACCOUNT
    cloudfront.__dict__["distribution_id"] = FAKE_DISTRIBUTION_ID
    own = cloudfront.bucket_policy_statement_should_contain
    return {
        "bucket_policy_statement": own,
        "bucket_policy_cases": [
            {
                "name": name,
                "current": current,
                "ensure": _policy_transition(
                    cf_context, current, "_ensure_bucket_policy"
                ),
                "delete": _policy_transition(
                    cf_context, current, "_delete_bucket_policy"
                ),
            }
            for name, current in _policy_cases(own)
        ],
    }


def _recorded(client_method: mock.MagicMock) -> dict[str, Any]:
    """mock の client method に渡された keyword 引数 (1 回だけ呼ばれている前提)"""
    client_method.assert_called_once()
    return dict(client_method.call_args.kwargs)


def _rds_cluster_call(rds_context, *, static: bool) -> dict[str, Any]:
    """cluster を作る API (作成か snapshot 復元) と、その引数"""
    rds = Rds.__new__(Rds)
    rds.context = rds_context
    rds._rds_client = mock.MagicMock()
    rds._rds_client.describe_db_clusters.return_value = {"DBClusters": []}
    with mock.patch.object(
        rds_module, "_generate_master_password", return_value=FAKE_MASTER_PASSWORD
    ):
        rds._create_or_restore_cluster(FAKE_RDS_SECURITY_GROUP_ID, static)
    for api in ("create_db_cluster", "restore_db_cluster_from_snapshot"):
        method = getattr(rds._rds_client, api)
        if method.called:
            return {"api": api, "args": _recorded(method)}
    raise click.ClickException("cluster を作る API が呼ばれませんでした")


def rds_documents(context: Context) -> dict[str, Any] | None:
    """RDS の作成 API に渡す引数 (managed = false は pocket が作らないので None)"""
    rds_context = context.rds
    if rds_context is None or not rds_context.managed:
        return None
    rds = Rds.__new__(Rds)
    rds.context = rds_context
    rds._rds_client = mock.MagicMock()
    rds._ec2_client = mock.MagicMock()
    rds._ec2_client.describe_security_groups.return_value = {"SecurityGroups": []}
    rds._rds_client.describe_db_instances.return_value = {"DBInstances": []}
    rds._ensure_subnet_group(FAKE_SUBNET_IDS)
    rds._ensure_security_group(FAKE_VPC_ID)
    rds._ensure_instance()
    # static の認証情報 (store に書く文字列)
    rds._rds_client.describe_db_clusters.return_value = {
        "DBClusters": [{"Endpoint": FAKE_RDS_ENDPOINT, "Port": 5432}]
    }
    with mock.patch.object(rds_module.secret_store, "put_stored_value") as put:
        rds._store_static_credentials(FAKE_MASTER_PASSWORD)
    plain = rds_context.model_copy(update={"snapshot_identifier": None})
    restored = rds_context.model_copy(update={"snapshot_identifier": FAKE_SNAPSHOT})
    return {
        "subnet_ids": FAKE_SUBNET_IDS,
        "vpc_id": FAKE_VPC_ID,
        "security_group_id": FAKE_RDS_SECURITY_GROUP_ID,
        "master_password": FAKE_MASTER_PASSWORD,
        "endpoint": json.loads(put.call_args.args[2])["host"],
        "port": 5432,
        "snapshot_identifier": FAKE_SNAPSHOT,
        "create_db_subnet_group": _recorded(rds._rds_client.create_db_subnet_group),
        "create_security_group": _recorded(rds._ec2_client.create_security_group),
        "create_db_instance": _recorded(rds._rds_client.create_db_instance),
        "create_cluster_static": _rds_cluster_call(plain, static=True)["args"],
        "create_cluster_managed": _rds_cluster_call(plain, static=False)["args"],
        "restore_cluster": _rds_cluster_call(restored, static=False)["args"],
        # この stage の設定で実際に呼ぶ API (snapshot の指定があれば復元)
        "cluster_api": _rds_cluster_call(
            rds_context,
            static=rds_context.password_strategy == "static",  # noqa: S105 戦略名
        )["api"],
        "rotation_rules": rds._desired_rotation_rules(),
        "static_credentials": put.call_args.args[2],
    }


def efs_documents(context: Context) -> dict[str, Any]:
    """EFS ごと (名前順) の作成引数と lifecycle policy"""
    result: dict[str, Any] = {}
    for container in context.container.values():
        vpc = container.vpc
        if vpc is None or vpc.efs is None or vpc.efs.name in result:
            continue
        efs = Efs.__new__(Efs)
        efs.context = vpc.efs
        efs.client = mock.MagicMock()
        efs.client.create_file_system.return_value = {"FileSystemId": "fs-vector"}
        with (
            mock.patch.object(Efs, "wait_status"),
            mock.patch.object(Efs, "ensure_lifecycle_policies"),
            contextlib.redirect_stdout(None),
        ):
            efs.create()
        args = _recorded(efs.client.create_file_system)
        # CreationToken は毎回変わる乱数
        args.pop("CreationToken")
        result[vpc.efs.name] = {
            "create_file_system": args,
            "lifecycle_policies": efs.lifecycle_policies_should_be,
        }
    return dict(sorted(result.items()))


def dsql_documents(context: Context) -> dict[str, Any] | None:
    if context.dsql is None:
        return None
    dsql = Dsql.__new__(Dsql)
    dsql.context = context.dsql
    dsql._client = mock.MagicMock()
    dsql._client.create_cluster.return_value = {"identifier": FAKE_DSQL_IDENTIFIER}
    dsql.__dict__["cluster"] = {"identifier": FAKE_DSQL_IDENTIFIER}
    with (
        mock.patch.object(Dsql, "_wait_active"),
        mock.patch.object(Dsql, "clear_cache"),
        mock.patch.object(Dsql, "publish_endpoint"),
    ):
        dsql.create()
    return {
        "identifier": FAKE_DSQL_IDENTIFIER,
        "create_cluster": _recorded(dsql._client.create_cluster),
        "endpoint": dsql.endpoint,
    }


def _plan_match_cases(document: dict[str, Any]) -> list[dict[str, Any]]:
    """今の plan (GetBackupPlan の応答) と、宣言どおりと判定されるか"""

    def variant(change) -> dict[str, Any]:
        current = copy.deepcopy(document)
        change(current)
        return current

    def completed(current: dict[str, Any]):
        # AWS が補完する項目 (比較しない)
        for index, rule in enumerate(current["Rules"]):
            rule["RuleId"] = "rule-%d" % index
            rule["StartWindowMinutes"] = 480
            rule["CompletionWindowMinutes"] = 10080
            rule["Lifecycle"]["OptInToArchiveForSupportedResources"] = False

    def first(current: dict[str, Any]) -> dict[str, Any]:
        return current["Rules"][0]

    cases = {
        "same": lambda c: None,
        "completed_by_aws": completed,
        "reordered": lambda c: c["Rules"].reverse(),
        "rule_removed": lambda c: c["Rules"].pop(),
        "rule_added": lambda c: c["Rules"].append(
            {**copy.deepcopy(first(c)), "RuleName": "vector-extra"}
        ),
        "rule_renamed": lambda c: first(c).update(RuleName="vector-renamed"),
        "schedule_changed": lambda c: first(c).update(
            ScheduleExpression="cron(0 0 1 1 ? 2000)"
        ),
        "timezone_changed": lambda c: first(c).update(
            ScheduleExpressionTimezone="Etc/GMT+12"
        ),
        "timezone_missing": lambda c: first(c).pop("ScheduleExpressionTimezone"),
        "vault_changed": lambda c: first(c).update(TargetBackupVaultName="other"),
        "delete_after_changed": lambda c: first(c)["Lifecycle"].update(
            DeleteAfterDays=first(c)["Lifecycle"]["DeleteAfterDays"] + 1
        ),
        "cold_storage_changed": lambda c: first(c)["Lifecycle"].update(
            MoveToColdStorageAfterDays=first(c)["Lifecycle"].get(
                "MoveToColdStorageAfterDays", 0
            )
            + 1
        ),
        "lifecycle_missing": lambda c: first(c).pop("Lifecycle"),
    }
    result = []
    for name, change in cases.items():
        current = variant(change)
        result.append(
            {
                "name": name,
                "current": current,
                "matches": _plan_matches(current, document),
            }
        )
    result.append(
        {"name": "empty", "current": {}, "matches": _plan_matches({}, document)}
    )
    return result


def backup_documents(context: Context) -> dict[str, Any] | None:
    backup_context = context.backup
    if backup_context is None:
        return None
    backup = Backup.__new__(Backup)
    backup.context = backup_context
    plans = []
    for plan in backup_context.plans:
        document = backup._plan_document(plan)
        backup._backup = mock.MagicMock()
        backup._backup.list_backup_selections.return_value = {
            "BackupSelectionsList": []
        }
        backup._ensure_selection(
            plan, "vector-plan-id", FAKE_BACKUP_ROLE_ARN, [FAKE_BACKUP_TARGET_ARN]
        )
        selection = _recorded(backup._backup.create_backup_selection)
        plans.append(
            {
                "plan_name": plan.plan_name,
                "document": document,
                "selection": selection["BackupSelection"],
                "match_cases": _plan_match_cases(document),
            }
        )
    return {
        "role_arn": FAKE_BACKUP_ROLE_ARN,
        "target_arn": FAKE_BACKUP_TARGET_ARN,
        "plans": plans,
    }


def documents(context: Context) -> dict[str, Any] | None:
    result = {
        "account_id": FAKE_ACCOUNT,
        "distribution_id": FAKE_DISTRIBUTION_ID,
        "s3": s3_documents(context) if context.s3 else None,
        "cloudfront": {
            name: cloudfront_documents(cf_context)
            for name, cf_context in context.cloudfront.items()
        },
        "rds": rds_documents(context),
        "efs": efs_documents(context),
        "dsql": dsql_documents(context),
        "backup": backup_documents(context),
    }
    used = [key for key, value in result.items() if key.endswith("_id") or value]
    if len(used) == 2:
        return None
    return result


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
            files["roles.json"] = (
                json.dumps(
                    [
                        role_entry(spec, pocket_context)
                        for spec in stage_role_specs(
                            pocket_context, account_id=FAKE_ACCOUNT
                        )
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
            docs = documents(pocket_context)
            if docs is not None:
                files["documents.json"] = (
                    json.dumps(docs, ensure_ascii=False, indent=2) + "\n"
                )
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
