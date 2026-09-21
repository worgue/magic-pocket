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

検知の限界 (false confidence を避けるため明記):
- メソッド呼び出しの receiver を同一ファイル内でしか追跡しない。関数引数で
  client を受け取るヘルパーは、引数の型注釈 (mypy-boto3-* の `IAMClient` 等)
  で service に紐づける。型注釈を付け忘れた client 引数は
  test_client_params_are_typed が検出する (AWS API メソッドを呼ぶ未注釈の
  引数を探す)。
- メソッド名→Action 名は機械的な PascalCase 変換。IAM Action と API 名が乖離する
  ケース (例: dsql:DbConnectAdmin) はワイルドカード宣言 (`dsql:*`) で吸収する。
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
    "AWS::CloudFront::Distribution": ["cloudfront:*"],
    "AWS::CloudFront::Function": ["cloudfront:*"],
    "AWS::CloudFront::KeyGroup": ["cloudfront:*"],
    "AWS::CloudFront::KeyValueStore": ["cloudfront:*"],
    "AWS::CloudFront::OriginAccessControl": ["cloudfront:*"],
    "AWS::CloudFront::PublicKey": ["cloudfront:*"],
    "AWS::CloudFront::ResponseHeadersPolicy": ["cloudfront:*"],
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


def find_uncovered_boto3(allowed: list[str]) -> dict[str, str]:
    """boto3 由来の必要 Action のうち allowed が包含しないものを返す。

    返り値: {問題の説明キー: 詳細}。空なら同期している。
    """
    services, calls = collect_boto3_usage()
    problems: dict[str, str] = {}
    for service in sorted(services):
        prefix = _SERVICE_TO_IAM_PREFIX.get(service, service)
        if not _prefix_covered(prefix, allowed):
            problems[f"prefix:{prefix}"] = (
                f"boto3.client('{service}') が使われているが、IAM prefix "
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
                f"deploy コードが {service}.{method}() を呼ぶが、"
                f"'{action}' が action_groups() でカバーされない"
            )
    return problems


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
