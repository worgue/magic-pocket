"""deploy コードと compute_actions() の同期検証。

`pocket/permissions.py` の action group は `docs/permissions/aws.md` を手書きで
コード化した「真実の源」だが、deploy コードが新しい AWS API / CFn リソース型を
触り始めても自動では追従せず、権限を絞った deploy ロールが本番で AccessDenied に
なる事故が繰り返された (route53:ListHostedZones / iam:TagRole /
cloudfront-keyvaluestore:*)。

必要 Action の発生源は 2 系統あり、それぞれ別の検知器でカバーする:

- (a) 明示 boto3 呼び出し駆動 → AST 解析 (`collect_boto3_usage`)。
  `pocket/` + `pocket_cli/` の `boto3.client("X")` と、そのクライアントへの
  メソッド呼び出しを抽出し、`action_groups()` 全グループの union が包含する
  ことを検証する。
- (b) CloudFormation 駆動 (リソース作成 / スタックタグ伝播) → 生テンプレートの
  `Type: AWS::*` 全抽出 (`collect_template_resource_types`)。リソース型ごとの
  必要 deploy Action を curate した表 (`RESOURCE_TYPE_ACTIONS`) と突き合わせ、
  **表に無い未知の型が現れたら fail** することで、新リソース型追加時に
  権限の検討を強制する。

比較対象が「union」(条件付き group も全部入り) なのは意図的: compute_actions()
は sm/ssm など条件排他で単一の最大構成が定義できず、過去の事故は全て
「どのグループにも未宣言」型だったため。条件分岐の正しさは
tests/test_permissions.py の per-condition テストの守備範囲。

- (c) Rust 版 CLI (`crates/pocket-cli/src`) の aws-sdk 呼び出し → テキスト走査
  (`collect_rust_usage`)。`.send()` / `.into_paginator()` で終わる method chain
  から operation を取り出し、(a) と同じ union に含まれることを検証する。
  SDK を使わず署名した HTTP で呼ぶ API は表 (`_RUST_SIGNED_HTTP`) で宣言する。

検知の限界 (false confidence を避けるため明記):
- メソッド呼び出しの receiver を同一ファイル内でしか追跡しない。関数引数で
  client を受け取るヘルパーは、引数の型注釈 (mypy-boto3-* の `IAMClient` 等)
  で service に紐づける。型注釈を付け忘れた client 引数は
  test_client_params_are_typed が検出する (AWS API メソッドを呼ぶ未注釈の
  引数を探す)。
- メソッド名→Action 名は機械的な PascalCase 変換。IAM Action と API 名が乖離する
  ケース (例: dsql:DbConnectAdmin) はワイルドカード宣言 (`dsql:*`) で吸収する。

Rust 側 (c) の検知の限界:
- 構文木ではなくテキストを走査する。追跡するのは `.send()` /
  `.into_paginator()` で終わる呼び出しだけで、operation は「その method chain
  (builder を変数に入れた場合は同じ関数の `let` 文) の中で、botocore の service
  model にある operation 名に当たる最初の method」とする。macro が展開して作る
  呼び出し、builder を関数の引数・戻り値・struct の field で受け渡す書き方、
  waiter (`.wait()`) は追跡できない。operation を特定できない `.send()` は
  test_rust_sends_all_resolved が落とすので、黙って漏れるのは `.send()` の形を
  取らない呼び出しだけ。
- service は型ではなく operation 名から決める。同じ名前の operation を持つ
  service が複数あるときは、そのファイルが名指しする `aws_sdk_*` crate で絞り、
  1 つに決まらなければ候補すべての Action を要求する (多めに要求する側に倒す)。
- operation 名は botocore の model (Rust の SDK と同じ Smithy model から生成)
  で判定する。botocore が知らない新しい API は「特定できない `.send()`」として
  落ちるので、botocore を上げる。
- 対象は deploy ロールで動く CLI だけ。Lambda の実行ロールで動く runtime lib
  (`crates/pocket-rs`) は対象外。
- Action 名は (a) と同じ機械的な変換で、条件付き group の正しさも見ない。
"""

from __future__ import annotations

import ast
import fnmatch
import re
from pathlib import Path

import botocore.session
from botocore import xform_name

from pocket.permissions import action_groups

_REPO = Path(__file__).resolve().parent.parent
_SCAN_ROOTS = [
    _REPO / "pocket",
    _REPO / "packages" / "magic-pocket-cli" / "pocket_cli",
]
_TEMPLATE_DIR = (
    _REPO
    / "packages"
    / "magic-pocket-cli"
    / "pocket_cli"
    / "templates"
    / "cloudformation"
)

# boto3 の service 名と IAM Action prefix が異なるもの
_SERVICE_TO_IAM_PREFIX = {
    "efs": "elasticfilesystem",
    "resourcegroupstaggingapi": "tag",
    "sesv2": "ses",
}

# AWS API 呼び出しではない boto3 client のメタメソッド
_META_METHODS = {"get_paginator", "get_waiter", "can_paginate", "close"}

# deploy ロールに不要な (service, method) の明示除外。
# 追加する場合は「なぜ不要か (runtime 専用 / 解析の誤帰属)」を必ずコメントで書くこと。
_EXCLUDED_CALLS: set[tuple[str, str]] = {
    # 解析の誤帰属: container_cli.list_secrets が同一関数内の
    # 同名ローカル変数 `client` を分岐で sm / ssm 両方に束縛するため、
    # get_secret_value が ssm 側にも帰属する。実際は secretsmanager 側の
    # 呼び出しで secretsmanager:* によりカバー済み。
    ("ssm", "get_secret_value"),
}

# CFn リソース型 → その作成/更新/削除/タグ伝播に deploy ロールが必要とする Action。
# 新しいリソース型をテンプレートに追加すると、この表に無い限り
# test_template_resource_types_all_known が fail する (= 権限の検討を強制する)。
# service:* で宣言済みのサービスはワイルドカード 1 つで足りる。
RESOURCE_TYPE_ACTIONS: dict[str, list[str]] = {
    "AWS::ApiGatewayV2::Api": ["apigateway:*"],
    "AWS::ApiGatewayV2::ApiGatewayManagedOverrides": ["apigateway:*"],
    "AWS::ApiGatewayV2::ApiMapping": ["apigateway:*"],
    "AWS::ApiGatewayV2::DomainName": ["apigateway:*"],
    "AWS::ApiGatewayV2::Integration": ["apigateway:*"],
    "AWS::ApiGatewayV2::Route": ["apigateway:*"],
    "AWS::CertificateManager::Certificate": [
        "acm:RequestCertificate",
        "acm:DescribeCertificate",
        "acm:DeleteCertificate",
    ],
    "AWS::CloudFront::CachePolicy": ["cloudfront:*"],
    "AWS::CloudFront::Distribution": ["cloudfront:*"],
    "AWS::CloudFront::Function": ["cloudfront:*"],
    "AWS::CloudFront::KeyGroup": ["cloudfront:*"],
    "AWS::CloudFront::KeyValueStore": ["cloudfront:*"],
    "AWS::CloudFront::OriginAccessControl": ["cloudfront:*"],
    "AWS::CloudFront::PublicKey": ["cloudfront:*"],
    "AWS::EC2::EIP": ["ec2:*"],
    "AWS::EC2::InternetGateway": ["ec2:*"],
    "AWS::EC2::NatGateway": ["ec2:*"],
    "AWS::EC2::Route": ["ec2:*"],
    "AWS::EC2::RouteTable": ["ec2:*"],
    "AWS::EC2::SecurityGroup": ["ec2:*"],
    "AWS::EC2::SecurityGroupIngress": ["ec2:*"],
    "AWS::EC2::Subnet": ["ec2:*"],
    "AWS::EC2::SubnetRouteTableAssociation": ["ec2:*"],
    "AWS::EC2::VPC": ["ec2:*"],
    "AWS::EC2::VPCGatewayAttachment": ["ec2:*"],
    "AWS::EFS::AccessPoint": ["elasticfilesystem:*"],
    "AWS::EFS::MountTarget": ["elasticfilesystem:*"],
    # スタックタグが taggable リソースへ伝播するため Tag/Untag/ListRoleTags も必要
    # (iam:TagRole 欠落事故 08a5b2a の再発防止対象)
    "AWS::IAM::Role": [
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:GetRole",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:PassRole",
        "iam:TagRole",
        "iam:UntagRole",
        "iam:ListRoleTags",
    ],
    "AWS::Lambda::EventSourceMapping": ["lambda:*"],
    "AWS::Lambda::Function": ["lambda:*"],
    "AWS::Lambda::Permission": ["lambda:*"],
    "AWS::Logs::LogGroup": ["logs:*"],
    # route53:ChangeResourceRecordSets 欠落事故 c5f33cc の再発防止対象
    "AWS::Route53::RecordSet": [
        "route53:ChangeResourceRecordSets",
        "route53:GetChange",
    ],
    "AWS::Scheduler::Schedule": ["scheduler:*", "iam:PassRole"],
    "AWS::SQS::Queue": ["sqs:*"],
    "AWS::SNS::Topic": ["sns:*"],
    "AWS::SNS::Subscription": ["sns:*"],
    "AWS::CloudWatch::Alarm": ["cloudwatch:*"],
    "AWS::WAFv2::IPSet": ["wafv2:*"],
    "AWS::WAFv2::WebACL": ["wafv2:*"],
}


def _allowed_actions() -> list[str]:
    """action_groups() 全グループの union (順序維持・重複除去)。"""
    seen: set[str] = set()
    union: list[str] = []
    for actions in action_groups().values():
        for action in actions:
            if action not in seen:
                seen.add(action)
                union.append(action)
    return union


def _action_covered(action: str, allowed: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(action, pattern) for pattern in allowed)


def _prefix_covered(prefix: str, allowed: list[str]) -> bool:
    return any(pattern.split(":", 1)[0] == prefix for pattern in allowed)


def _pascal(method: str) -> str:
    return "".join(part.capitalize() for part in method.split("_"))


def _client_service(node: ast.AST) -> str | None:
    """node が boto3.client("X") / boto3.resource("X") なら service 名を返す。"""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr in ("client", "resource")):
        return None
    if not (isinstance(func.value, ast.Name) and func.value.id == "boto3"):
        return None
    if node.args and isinstance(node.args[0], ast.Constant):
        value = node.args[0].value
        if isinstance(value, str):
            return value
    return None


def _receiver_key(node: ast.AST) -> str | None:
    """client を保持しうる receiver (x / self.x) を追跡キーに正規化する。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id == "self":
            return node.attr
    return None


def _value_services(node: ast.AST, bound: dict[str, set[str]]) -> set[str]:
    """代入/return の右辺が表す client の service 集合を解決する。

    `boto3.client("svc")` 直接、または既知の束縛名の呼び出し
    (`self.get_client()` 等) を 1 ホップ解決する。`a if c else b` は両辺の和。
    """
    direct = _client_service(node)
    if direct:
        return {direct}
    if isinstance(node, ast.IfExp):
        return _value_services(node.body, bound) | _value_services(node.orelse, bound)
    if isinstance(node, ast.Call):
        key = _receiver_key(node.func)
        if key and key in bound:
            return set(bound[key])
    return set()


def _boto3_type_names(tree: ast.AST) -> dict[str, str]:
    """`from mypy_boto3_iam import IAMClient` 等の import から {名前: service}。

    `import mypy_boto3_iam` の module 名自体も登録する (`mypy_boto3_iam.IAMClient`
    の形の注釈用)。
    """
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root.startswith(_BOTO3_TYPES_PREFIX):
                service = _boto3_types_service(root)
                for alias in node.names:
                    names[alias.asname or alias.name] = service
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root.startswith(_BOTO3_TYPES_PREFIX):
                    names[alias.asname or root] = _boto3_types_service(root)
    return names


_BOTO3_TYPES_PREFIX = "mypy_boto3_"


def _boto3_types_service(module: str) -> str:
    """mypy_boto3_cloudfront_keyvaluestore → cloudfront-keyvaluestore"""
    return module.removeprefix(_BOTO3_TYPES_PREFIX).replace("_", "-")


def _annotation_services(annotation: ast.AST | None, types: dict[str, str]) -> set[str]:
    """引数の型注釈 (`IAMClient` / `S3Client | None` / 文字列注釈) の service。"""
    if annotation is None:
        return set()
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return _annotation_services(ast.parse(annotation.value, mode="eval"), types)
    found: set[str] = set()
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name) and node.id in types:
            found.add(types[node.id])
    return found


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    return fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs


def collect_boto3_usage() -> tuple[set[str], set[tuple[str, str]]]:  # noqa: C901 AST 走査の網羅分岐が本質的に多い検査用ヘルパー
    """(全 service 名, 追跡できた (service, method) ペア) を返す。

    追跡対象:
    - `self.x = boto3.client("svc")` / `return boto3.client("svc")` する
      関数・プロパティ (get_client パターン) — ファイル全体で有効。
      `self.client = self.get_client()` の 1 ホップ間接も解決する
    - `x = boto3.client("svc")` のローカル変数 — **関数スコープ内でのみ**有効
      (別関数の同名変数 `client` 等に誤帰属させない)
    - `boto3.client("svc").method(...)` の直接チェーン
    - `client.get_paginator("operation")` は operation を method として記録
    - `IAMClient` 等 (mypy-boto3-*) の型注釈が付いた関数引数 — その関数
      スコープ内で有効。型注釈の無い client 引数は
      test_client_params_are_typed が落とす
    """
    services: set[str] = set()
    calls: set[tuple[str, str]] = set()

    def scan_calls(scope: ast.AST, bound: dict[str, set[str]]) -> None:
        for node in ast.walk(scope):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            method = node.func.attr
            receiver = node.func.value
            resolved = _value_services(receiver, bound)
            if not resolved:
                key = _receiver_key(receiver)
                if key and key in bound:
                    resolved = set(bound[key])
            for service in resolved:
                if method == "get_paginator":
                    if node.args and isinstance(node.args[0], ast.Constant):
                        calls.add((service, str(node.args[0].value)))
                elif method not in _META_METHODS:
                    calls.add((service, method))

    for root in _SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))

            for node in ast.walk(tree):
                service = _client_service(node)
                if service:
                    services.add(service)

            # ファイル全体で有効な束縛 (self.attr / 関数 return)。
            # `self.client = self.get_client()` の間接を解決するため 2 周する
            bound_global: dict[str, set[str]] = {}
            for _ in range(2):
                for node in ast.walk(tree):
                    if isinstance(node, ast.Assign):
                        svcs = _value_services(node.value, bound_global)
                        if svcs:
                            for target in node.targets:
                                if (
                                    isinstance(target, ast.Attribute)
                                    and isinstance(target.value, ast.Name)
                                    and target.value.id == "self"
                                ):
                                    bound_global.setdefault(target.attr, set()).update(
                                        svcs
                                    )
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        for sub in ast.walk(node):
                            if isinstance(sub, ast.Return) and sub.value is not None:
                                svcs = _value_services(sub.value, bound_global)
                                if svcs:
                                    bound_global.setdefault(node.name, set()).update(
                                        svcs
                                    )

            # 関数ごとにローカル変数の束縛を重ねて呼び出しを解決する
            functions = [
                n
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            types = _boto3_type_names(tree)
            for fn in functions:
                local = {k: set(v) for k, v in bound_global.items()}
                # client を引数で受け取るヘルパー (iam_roles.ensure_role 等) は
                # 引数の型注釈で service に紐づける
                for arg in _params(fn):
                    svcs = _annotation_services(arg.annotation, types)
                    if svcs:
                        services.update(svcs)
                        local.setdefault(arg.arg, set()).update(svcs)
                for sub in ast.walk(fn):
                    if isinstance(sub, ast.Assign):
                        svcs = _value_services(sub.value, local)
                        if svcs:
                            for target in sub.targets:
                                if isinstance(target, ast.Name):
                                    local.setdefault(target.id, set()).update(svcs)
                scan_calls(fn, local)

    return services, calls


def _boto3_operation_methods(services: set[str]) -> set[str]:
    """services の boto3 client が持つ API メソッド名 (create_role 等)。"""
    session = botocore.session.get_session()
    methods: set[str] = set()
    for service in services:
        model = session.get_service_model(service)
        methods.update(xform_name(op) for op in model.operation_names)
    return methods | {"get_paginator", "get_waiter"}


def find_untyped_client_params(roots: list[Path] | None = None) -> list[str]:
    """型注釈の無い引数のうち、boto3 client として使われているものを返す。

    `param.<AWS API メソッド>(...)` の呼び出しがあれば client とみなす
    (メソッド名は botocore の service model から引くので、名前の規約に依存
    しない)。こうした引数は collect_boto3_usage が追跡できず、必要 Action の
    検知から黙って漏れる。
    """
    services, _calls = collect_boto3_usage()
    operations = _boto3_operation_methods(services)
    found: list[str] = []
    for root in roots or _SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                untyped = {
                    arg.arg
                    for arg in _params(fn)
                    if arg.annotation is None and arg.arg not in ("self", "cls")
                }
                for node in ast.walk(fn):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id in untyped
                        and node.func.attr in operations
                    ):
                        found.append(
                            f"{path}:{fn.lineno} {fn.name}({node.func.value.id})"
                            f".{node.func.attr}"
                        )
    return sorted(set(found))


def collect_template_resource_types() -> set[str]:
    """生 CFn テンプレートから全 `Type: AWS::*` を抽出する。

    jinja2 レンダリングを通すと、その構成に含まれない分岐 ({% if %}) の
    リソース型を取りこぼすため、意図的に生テキストから抽出する。
    """
    types: set[str] = set()
    for path in sorted(_TEMPLATE_DIR.glob("*.yaml")):
        types |= set(
            re.findall(r"Type:\s*['\"]?(AWS::\S+?)['\"]?\s*$", path.read_text(), re.M)
        )
    return types


def _find_uncovered_calls(
    services: set[str],
    calls: set[tuple[str, str]],
    allowed: list[str],
    origin: str,
) -> dict[str, str]:
    """(service, method) 由来の必要 Action のうち allowed が包含しないもの。"""
    problems: dict[str, str] = {}
    for service in sorted(services):
        prefix = _SERVICE_TO_IAM_PREFIX.get(service, service)
        if not _prefix_covered(prefix, allowed):
            problems[f"prefix:{prefix}"] = (
                f"{origin} が service '{service}' を使っているが、IAM prefix "
                f"'{prefix}:' の Action が action_groups() のどこにも無い"
            )
    for service, method in sorted(calls):
        if (service, method) in _EXCLUDED_CALLS:
            continue
        prefix = _SERVICE_TO_IAM_PREFIX.get(service, service)
        if not _prefix_covered(prefix, allowed):
            continue  # prefix レベルで既に報告済み
        action = f"{prefix}:{_pascal(method)}"
        if not _action_covered(action, allowed):
            problems[f"action:{action}"] = (
                f"{origin} が {service}.{method}() を呼ぶが、"
                f"'{action}' が action_groups() でカバーされない"
            )
    return problems


def find_uncovered_boto3(allowed: list[str]) -> dict[str, str]:
    """boto3 由来の必要 Action のうち allowed が包含しないものを返す。

    返り値: {問題の説明キー: 詳細}。空なら同期している。
    """
    services, calls = collect_boto3_usage()
    return _find_uncovered_calls(services, calls, allowed, "deploy コード (Python)")


def find_uncovered_template_actions(allowed: list[str]) -> dict[str, str]:
    """CFn テンプレ由来の必要 Action のうち allowed が包含しないものを返す。"""
    problems: dict[str, str] = {}
    for rtype in sorted(collect_template_resource_types()):
        for action in RESOURCE_TYPE_ACTIONS.get(rtype, []):
            if not _action_covered(action, allowed):
                problems[f"action:{action}"] = (
                    f"CFn テンプレに {rtype} があり '{action}' が必要だが、"
                    f"action_groups() でカバーされない"
                )
    return problems


# ---------------------------------------------------------------------------
# Rust 版 CLI (crates/pocket-cli) の aws-sdk 呼び出し
# ---------------------------------------------------------------------------

_RUST_SRC = _REPO / "crates" / "pocket-cli" / "src"

# aws-sdk の crate 名 (aws_sdk_<x>) と botocore の service 名が異なるもの
_RUST_CRATE_TO_SERVICE = {
    "cloudfrontkeyvaluestore": "cloudfront-keyvaluestore",
    "cloudwatchlogs": "logs",
    "resourcegroupstagging": "resourcegroupstaggingapi",
}

# AWS の API ではない `.send()` (reqwest 等)。キーは (src からの相対パス, 関数名)。
# 追加する場合は「何を呼んでいるか」を必ずコメントで書くこと。
_RUST_NON_AWS_SENDS: set[tuple[str, str]] = {
    # Neon の API (reqwest)
    ("resource/neon_api.rs", "raw"),
    # 自分の global IP を調べる HTTP (checkip 等。reqwest)
    ("waf.rs", "fetch_text"),
    # 署名した HTTP で呼ぶ CloudFront の API。Action は _RUST_SIGNED_HTTP で宣言する
    ("deploy/upload.rs", "create_invalidation"),
}

# SDK を使わず、署名した HTTP (aws_sigv4) で直接呼ぶ API。
# `aws_sigv4` を使うファイルがここに無いと test_rust_signed_http_declared が落ちる。
# ファイルの中で呼ぶ API を増やしたときは、ここに Action を足す (機械では追えない)。
_RUST_SIGNED_HTTP: dict[str, list[str]] = {
    "deploy/upload.rs": ["cloudfront:CreateInvalidation"],
}

_RUST_SEND = re.compile(r"\.\s*(?:send|into_paginator)\s*\(\s*\)")
_RUST_METHOD = re.compile(r"\.\s*((?:r#)?\w+)\s*\(")
_RUST_BLANK = re.compile(r"[^\n]")


def _rust_code_only(src: str) -> str:
    """コメント・文字列・文字 literal の中身を空白にする (位置と行は保つ)。"""
    pattern = re.compile(
        r"//[^\n]*"
        r"|/\*.*?\*/"
        r'|b?r(#*)".*?"\1'
        r'|b?"(?:\\.|[^"\\])*"'
        r"|b?'(?:\\.|[^'\\])'",
        re.S,
    )
    return pattern.sub(lambda m: _RUST_BLANK.sub(" ", m.group(0)), src)


def _rust_open_paren(code: str, close: int) -> int:
    """close にある `)` に対応する `(` の位置。"""
    depth = 0
    i = close
    while True:
        if code[i] == ")":
            depth += 1
        elif code[i] == "(":
            depth -= 1
            if depth == 0:
                return i
        i -= 1


def _rust_chain(code: str, end: int) -> tuple[list[str], str]:
    """end (`.send` の `.` の位置) で終わる method chain を逆向きに辿る。

    返り値: (chain の method 名 (呼び出し順), chain の根の式の末尾の識別子)。
    `self.client(region).create_cluster().send()` なら
    (["client", "create_cluster"], "self")。
    """
    methods: list[str] = []
    pos = end
    while True:
        i = pos - 1
        while code[i].isspace():
            i -= 1
        if code[i] != ")":
            break
        i = _rust_open_paren(code, i)
        j = i - 1
        while code[j].isalnum() or code[j] in "_#":
            j -= 1
        k = j
        while code[k].isspace():
            k -= 1
        name = code[j + 1 : i]
        if code[k] != "." or not name:
            break  # method ではなく関数の呼び出し (`Client::new(..)` 等) が根
        methods.append(name.removeprefix("r#"))
        pos = k
    root = re.search(r"(\w+)\s*$", code[:pos])
    return methods[::-1], root.group(1) if root else ""


def _rust_enclosing_fn(code: str, pos: int) -> tuple[str, int]:
    """pos を含む (直前に始まる) 関数の名前と開始位置。"""
    found = ("", 0)
    for m in re.finditer(r"\bfn\s+(\w+)", code[:pos]):
        found = (m.group(1), m.start())
    return found


def _rust_services(code: str) -> set[str]:
    crates: list[str] = re.findall(r"\baws_sdk_([a-z0-9]+)\b", code)
    return {_RUST_CRATE_TO_SERVICE.get(crate, crate) for crate in crates}


def collect_rust_usage(
    root: Path | None = None,
) -> tuple[set[str], set[tuple[str, str]], list[str]]:
    """(全 service 名, (service, operation) ペア, 特定できない `.send()`) を返す。

    方式と限界は module の docstring の「Rust 側 (c)」を参照。
    """
    root = root or _RUST_SRC
    sources = {
        str(path.relative_to(root)): _rust_code_only(path.read_text())
        for path in sorted(root.rglob("*.rs"))
    }
    services: set[str] = set()
    for code in sources.values():
        services |= _rust_services(code)
    session = botocore.session.get_session()
    operations = {
        service: {
            xform_name(op) for op in session.get_service_model(service).operation_names
        }
        for service in services
    }

    def known(methods: list[str]) -> list[str]:
        return [m for m in methods if any(m in ops for ops in operations.values())]

    calls: set[tuple[str, str]] = set()
    unresolved: list[str] = []
    for rel, code in sources.items():
        named = _rust_services(code)
        for m in _RUST_SEND.finditer(code):
            methods, var = _rust_chain(code, m.start())
            fn_name, fn_start = _rust_enclosing_fn(code, m.start())
            found = known(methods)
            if not found and var:
                # builder を変数に入れてから send する書き方。同じ関数の let 文を探す
                lets = re.findall(
                    rf"\blet\s+(?:mut\s+)?{re.escape(var)}\s*=([^;]*);",
                    code[fn_start : m.start()],
                )
                if lets:
                    found = known(_RUST_METHOD.findall(lets[-1]))
            if not found:
                if (rel, fn_name) not in _RUST_NON_AWS_SENDS:
                    line = code.count("\n", 0, m.start()) + 1
                    unresolved.append(f"{rel}:{line} fn {fn_name}")
                continue
            operation = found[0].removeprefix("r#")
            candidates = {s for s, ops in operations.items() if operation in ops}
            if len(candidates) > 1 and candidates & named:
                candidates &= named
            calls.update((service, operation) for service in candidates)
    return services, calls, unresolved


def find_uncovered_rust(allowed: list[str]) -> dict[str, str]:
    """Rust 版 CLI 由来の必要 Action のうち allowed が包含しないものを返す。"""
    services, calls, _unresolved = collect_rust_usage()
    problems = _find_uncovered_calls(services, calls, allowed, "Rust 版 CLI")
    for rel, actions in sorted(_RUST_SIGNED_HTTP.items()):
        for action in actions:
            if not _action_covered(action, allowed):
                problems[f"action:{action}"] = (
                    f"Rust 版 CLI の {rel} が署名した HTTP で '{action}' を呼ぶが、"
                    "action_groups() でカバーされない"
                )
    return problems


def find_undeclared_signed_http(root: Path | None = None) -> list[str]:
    """`aws_sigv4` を使うのに _RUST_SIGNED_HTTP に無いファイル。"""
    root = root or _RUST_SRC
    return sorted(
        rel
        for path in sorted(root.rglob("*.rs"))
        if re.search(r"\baws_sigv4\b", _rust_code_only(path.read_text()))
        and (rel := str(path.relative_to(root))) not in _RUST_SIGNED_HTTP
    )


# ---------------------------------------------------------------------------
# 同期テスト本体
# ---------------------------------------------------------------------------


def test_boto3_service_prefixes_covered():
    """deploy コードが触る全 service prefix が action group に宣言されている。

    cloudfront-keyvaluestore (8f6de94) / dsql / tag の「新 service prefix の
    取りこぼし」クラスの再発防止。
    """
    problems = {
        k: v
        for k, v in find_uncovered_boto3(_allowed_actions()).items()
        if k.startswith("prefix:")
    }
    assert not problems, "\n".join(problems.values())


def test_boto3_method_actions_covered():
    """追跡できた boto3 メソッド呼び出しの Action が宣言されている。

    route53:ListHostedZones (c5f33cc) の「granular 宣言サービスへの新メソッド」
    クラスの再発防止。
    """
    problems = {
        k: v
        for k, v in find_uncovered_boto3(_allowed_actions()).items()
        if k.startswith("action:")
    }
    assert not problems, "\n".join(problems.values())


def test_template_resource_types_all_known():
    """CFn テンプレの全リソース型が RESOURCE_TYPE_ACTIONS に登録されている。

    新しいリソース型をテンプレートに足すとここで fail する。
    → 必要な deploy Action を検討し、表 (と必要なら permissions.py /
      docs/permissions/aws.md) に追加すること。
    """
    unknown = collect_template_resource_types() - set(RESOURCE_TYPE_ACTIONS)
    assert not unknown, (
        f"RESOURCE_TYPE_ACTIONS に未登録の CFn リソース型: {sorted(unknown)}。"
        "必要な deploy Action を検討して表に追加してください"
    )


def test_template_actions_covered():
    """CFn リソース型由来の必要 Action が宣言されている。

    iam:TagRole (08a5b2a) の「CFn タグ伝播」クラスの再発防止。
    """
    problems = find_uncovered_template_actions(_allowed_actions())
    assert not problems, "\n".join(problems.values())


def test_client_params_are_typed():
    """boto3 client を受け取る引数には mypy-boto3-* の型注釈を付ける。

    型注釈が無いと collect_boto3_usage がその引数経由の呼び出しを追跡できず、
    必要 Action が検知から漏れる。
    例: `def ensure_role(iam_client: IAMClient, ...)` (import は TYPE_CHECKING 内。
    新しい service なら pyproject.toml の boto3-stubs-lite の extras に足す)
    client でない引数が API と同名のメソッドを持つ場合 (jinja2 の
    `Environment.get_template` 等) も、その本来の型を注釈すれば対象外になる。
    """
    untyped = find_untyped_client_params()
    assert not untyped, "型注釈の無い client 引数:\n" + "\n".join(untyped)


def test_untyped_client_param_detector(tmp_path):
    """未注釈の client 引数を検出し、注釈付き・client 以外の型は対象外にする。"""
    (tmp_path / "helpers.py").write_text(
        "from mypy_boto3_iam import IAMClient\n"
        "from jinja2 import Environment\n"
        "def untyped(client, name):\n"
        "    client.create_role(RoleName=name)\n"
        "def typed(client: IAMClient, name):\n"
        "    client.create_role(RoleName=name)\n"
        "def paginated(ec2):\n"
        "    ec2.get_paginator('describe_nat_gateways')\n"
        "def not_a_client(env: Environment):\n"
        "    env.get_template('x')\n"
    )
    assert find_untyped_client_params([tmp_path]) == [
        f"{tmp_path / 'helpers.py'}:3 untyped(client).create_role",
        f"{tmp_path / 'helpers.py'}:7 paginated(ec2).get_paginator",
    ]


def test_analyzer_tracks_known_callsites():
    """AST 解析器が既知の代表的呼び出しを実際に捕捉していることの自己検証。

    解析器が壊れて何も抽出しなくなると上のテストが空集合で green になる
    (false confidence) ため、実在する呼び出しの捕捉を明示的に確認する。
    """
    services, calls = collect_boto3_usage()
    # 過去事故 #1: pocket/utils.py の明示呼び出し
    assert ("route53", "list_hosted_zones") in calls
    # 過去事故 #3: 多行 client 生成 (cloudfront.py:149)
    assert "cloudfront-keyvaluestore" in services
    # get_client() パターン (cloudformation.py)
    assert ("cloudformation", "create_stack") in calls
    # paginator パターン (dsql.py)
    assert ("dsql", "list_clusters") in calls
    # self.x = boto3.client パターン + resourcegroupstaggingapi
    assert ("resourcegroupstaggingapi", "tag_resources") in calls
    # 型注釈付きの client 引数 (iam_roles.py)
    assert ("iam", "create_role") in calls


def test_rust_actions_covered():
    """Rust 版 CLI が呼ぶ API の Action と service prefix が宣言されている。

    manifest に無い API を Rust 版 CLI から呼ぶとここで fail する。
    → pocket/permissions.py (と docs/permissions/aws.md) に足し、`just vectors` で
      Rust 側の写し (crates/pocket-cli/src/permissions.rs) も直す。
    """
    problems = find_uncovered_rust(_allowed_actions())
    assert not problems, "\n".join(problems.values())


def test_rust_sends_all_resolved():
    """Rust 版 CLI の `.send()` / `.into_paginator()` はすべて operation が分かる。

    特定できない呼び出しは必要 Action の検知から漏れる。AWS の API なら
    `client.operation()...send()` の形 (builder を変数に入れるなら同じ関数の
    `let`) に書き直す。AWS の API でないなら _RUST_NON_AWS_SENDS に理由を書いて足す。
    """
    _services, _calls, unresolved = collect_rust_usage()
    assert not unresolved, "operation を特定できない send:\n" + "\n".join(unresolved)


def test_rust_non_aws_sends_exist():
    """_RUST_NON_AWS_SENDS の除外が指す関数は実在する (古い除外を残さない)。"""
    stale = [
        f"{rel} fn {fn}"
        for rel, fn in sorted(_RUST_NON_AWS_SENDS)
        if not re.search(
            rf"\bfn\s+{re.escape(fn)}\b", _rust_code_only((_RUST_SRC / rel).read_text())
        )
    ]
    assert not stale, "実在しない除外:\n" + "\n".join(stale)


def test_rust_signed_http_declared():
    """`aws_sigv4` で署名した HTTP を使うファイルは _RUST_SIGNED_HTTP に宣言する。"""
    undeclared = find_undeclared_signed_http()
    assert not undeclared, (
        f"_RUST_SIGNED_HTTP に未宣言のファイル: {undeclared}。"
        "呼ぶ API の Action を表に書いてください"
    )
    stale = [
        rel
        for rel in _RUST_SIGNED_HTTP
        if not re.search(r"\baws_sigv4\b", (_RUST_SRC / rel).read_text())
    ]
    assert not stale, f"aws_sigv4 を使っていないファイルの宣言: {stale}"


def test_rust_analyzer_tracks_known_callsites():
    """Rust の走査が代表的な書き方を実際に捕捉していることの自己検証。"""
    services, calls, _unresolved = collect_rust_usage()
    # struct の field の client (stack/ops.rs)
    assert ("cloudformation", "create_stack") in calls
    # client を返す method 経由 + builder を変数に入れてから send (deploy/dsql.rs)
    assert ("dsql", "create_cluster") in calls
    # chain の途中に r#type がある (deploy/stored_value.rs)
    assert ("ssm", "put_parameter") in calls
    # paginator
    assert ("s3", "list_objects_v2") in calls
    # `Client::new(..)` から直接呼ぶ + feature `ec2` の後ろ (ec2.rs)
    assert ("ec2", "create_security_group") in calls
    # crate 名と service 名が違う
    assert "cloudfront-keyvaluestore" in services
    assert ("resourcegroupstaggingapi", "tag_resources") in calls
    assert ("logs", "filter_log_events") in calls


def test_rust_analyzer_detector(tmp_path):
    """走査の単体検証: 解決・曖昧な operation・特定できない send・署名 HTTP。"""
    (tmp_path / "a.rs").write_text(
        "use aws_sdk_iam::Client;\n"
        "// client.delete_role().send() はコメントなので数えない\n"
        "async fn direct(client: &Client) {\n"
        '    client.create_role().role_name("x)").send().await;\n'
        "}\n"
        "async fn builder(client: &Client) {\n"
        "    let mut request = client.list_roles();\n"
        "    request = request.max_items(1);\n"
        "    request.send().await;\n"
        "}\n"
        "async fn other(http: &reqwest::Client) {\n"
        '    http.get("https://example.com").send().await;\n'
        "}\n"
    )
    (tmp_path / "b.rs").write_text(
        "fn clients() -> (aws_sdk_dsql::Client, aws_sdk_backup::Client) { todo!() }\n"
        "async fn tag(c: &aws_sdk_dsql::Client) {\n"
        "    c.tag_resource().send().await;\n"
        "}\n"
        "async fn pages(c: &aws_sdk_dsql::Client) {\n"
        "    c.list_clusters().into_paginator().send();\n"
        "}\n"
        "use aws_sigv4::sign::v4;\n"
    )
    services, calls, unresolved = collect_rust_usage(tmp_path)
    assert services == {"iam", "dsql", "backup"}
    assert calls == {
        ("iam", "create_role"),
        ("iam", "list_roles"),
        # dsql と backup の両方にあり、ファイルが両方を名指しするので両方を要求する
        ("dsql", "tag_resource"),
        ("backup", "tag_resource"),
        ("dsql", "list_clusters"),
    }
    assert unresolved == ["a.rs:12 fn other"]
    assert find_undeclared_signed_http(tmp_path) == ["b.rs"]


# ---------------------------------------------------------------------------
# 回帰: 既知の事故ケースを意図的に欠落させると検知が fail する
# ---------------------------------------------------------------------------


def _allowed_without(*patterns: str) -> list[str]:
    return [a for a in _allowed_actions() if a not in patterns]


def test_regression_route53_list_hosted_zones():
    """過去事故 #1 (c5f33cc): 宣言から消すと boto3 解析が検知する。"""
    problems = find_uncovered_boto3(_allowed_without("route53:ListHostedZones"))
    assert "action:route53:ListHostedZones" in problems


def test_regression_iam_tag_role():
    """過去事故 #2 (08a5b2a): 宣言から消すと CFn 解析が検知する。"""
    problems = find_uncovered_template_actions(
        _allowed_without("iam:TagRole", "iam:UntagRole", "iam:ListRoleTags")
    )
    assert "action:iam:TagRole" in problems


def test_regression_cloudfront_keyvaluestore():
    """過去事故 #3 (8f6de94): 宣言から消すと prefix レベルで検知する。"""
    problems = find_uncovered_boto3(_allowed_without("cloudfront-keyvaluestore:*"))
    assert "prefix:cloudfront-keyvaluestore" in problems


def test_regression_dsql():
    """実装着手時に発見したギャップ #4: dsql:* を消すと検知する。"""
    problems = find_uncovered_boto3(_allowed_without("dsql:*"))
    assert "prefix:dsql" in problems


def test_regression_tag():
    """ギャップ #5: tag 系を消すと検知する (resourcegroupstaggingapi)。"""
    problems = find_uncovered_boto3(
        _allowed_without("tag:TagResources", "tag:UntagResources")
    )
    assert "prefix:tag" in problems


def test_regression_scheduler():
    """ギャップ #6: scheduler:* を消すと CFn 解析が検知する。"""
    problems = find_uncovered_template_actions(_allowed_without("scheduler:*"))
    assert "action:scheduler:*" in problems


def test_regression_iam_list_role_policies():
    """ギャップ #7 (本検知器の初回実行で発見): ロール削除時の inline policy
    列挙 (iam_roles.delete_role)。消すと boto3 解析が検知する。"""
    problems = find_uncovered_boto3(_allowed_without("iam:ListRolePolicies"))
    assert "action:iam:ListRolePolicies" in problems


def test_regression_rds_ssm_delete_parameter():
    """ギャップ #8 (本検知器の初回実行で発見): RDS static password の
    SSM パラメータ削除 (rds.py)。消すと boto3 解析が検知する。
    DeleteParameters (複数形, ssm group) では DeleteParameter (単数形) を
    カバーできない点に注意。"""
    problems = find_uncovered_boto3(_allowed_without("ssm:DeleteParameter"))
    assert "action:ssm:DeleteParameter" in problems


def test_regression_rust_only_action():
    """Rust 版 CLI の呼び出しも、宣言から消すと検知する (署名 HTTP の宣言を含む)。"""
    problems = find_uncovered_rust(_allowed_without("iam:ListRolePolicies"))
    assert "action:iam:ListRolePolicies" in problems
    problems = find_uncovered_rust(_allowed_without("cloudfront:*"))
    assert "action:cloudfront:CreateInvalidation" in problems
