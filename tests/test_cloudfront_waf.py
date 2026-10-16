"""CloudFront WAF (IP allowlist + 高頻度操作 CLI) 関連のテスト。

- pocket.toml schema: [cloudfront.<name>.waf] block と extra="forbid" の効き方
- CFn 出力: CloudFrontWafStack が IPSet + WebACL を生成
- CloudFront distribution: WAF ARN が WebACLId として attach される
- WAF 未設定時の回帰: 既存 yaml に差分が出ない
"""

from __future__ import annotations

from unittest import mock

import pytest
from pocket_cli.resources.aws.cloudformation import (
    CloudFrontStack,
    CloudFrontWafStack,
)
from pydantic import ValidationError

from pocket.context import CloudFrontContext, CloudFrontWafContext, RouteContext
from pocket.settings import CloudFront as CloudFrontSettings
from pocket.settings import CloudFrontWaf

# ---------------------------------------------------------------------------
# pocket.toml schema
# ---------------------------------------------------------------------------


def test_waf_block_accepts_empty():
    """空の [cloudfront.web.waf] block でも valid (IP allowlist モード既定)。"""
    waf = CloudFrontWaf.model_validate({})
    assert waf.enable_ip_set is True
    assert waf.managed_rule_groups == []


def test_waf_block_accepts_managed_rule_groups():
    waf = CloudFrontWaf.model_validate(
        {"managed_rule_groups": ["AWSManagedRulesCommonRuleSet"]}
    )
    assert waf.managed_rule_groups == ["AWSManagedRulesCommonRuleSet"]


def test_waf_rejects_ip_allow_list_default():
    """toml に IP リテラルを書くと validation error。

    真実源を CLI 一本に絞り、`pocket.toml` と IPSet が二系統で drift する事故を防ぐ。
    """
    with pytest.raises(ValidationError) as exc:
        CloudFrontWaf.model_validate({"ip_allow_list_default": ["203.0.113.0/24"]})
    assert "extra" in str(exc.value).lower() or "forbidden" in str(exc.value).lower()


def test_waf_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        CloudFrontWaf.model_validate({"foo_bar": True})


def test_waf_rejects_no_rule_at_all():
    """enable_ip_set=false + managed_rule_groups=[] は WAF が何もしないので reject。"""
    with pytest.raises(ValidationError) as exc:
        CloudFrontWaf.model_validate({"enable_ip_set": False})
    assert "managed_rule_groups" in str(exc.value)


def test_waf_managed_rules_only_is_valid():
    """enable_ip_set=false でも managed_rule_groups があれば valid。"""
    waf = CloudFrontWaf.model_validate(
        {
            "enable_ip_set": False,
            "managed_rule_groups": ["AWSManagedRulesCommonRuleSet"],
        }
    )
    assert waf.enable_ip_set is False
    assert waf.managed_rule_groups == ["AWSManagedRulesCommonRuleSet"]


def test_cloudfront_settings_with_waf():
    cf = CloudFrontSettings.model_validate(
        {
            "routes": [{"is_default": True, "is_spa": True, "origin_path": "/main"}],
            "waf": {"managed_rule_groups": ["AWSManagedRulesCommonRuleSet"]},
        }
    )
    assert cf.waf is not None
    assert cf.waf.managed_rule_groups == ["AWSManagedRulesCommonRuleSet"]


def test_cloudfront_settings_without_waf_keeps_default_none():
    cf = CloudFrontSettings.model_validate(
        {"routes": [{"is_default": True, "is_spa": True, "origin_path": "/main"}]}
    )
    assert cf.waf is None


# ---------------------------------------------------------------------------
# CFn template: CloudFrontWafStack
# ---------------------------------------------------------------------------


def _make_cf_context(*, waf: CloudFrontWafContext | None) -> CloudFrontContext:
    return CloudFrontContext(
        name="web",
        region="ap-northeast-1",
        s3_region="ap-northeast-1",
        stage="dev",
        slug="dev-testprj-web",
        bucket_name="dev-testprj-bucket",
        resource_prefix="dev-testprj-",
        routes=[RouteContext(is_default=True, origin_path="/app")],
        waf=waf,
    )


def test_waf_stack_name_is_us_east_1_scoped():
    ctx = _make_cf_context(waf=CloudFrontWafContext())
    with mock.patch("boto3.client") as m:
        CloudFrontWafStack(ctx)
        # us-east-1 client が初期化されている
        m.assert_called_once_with("cloudformation", region_name="us-east-1")


def test_waf_stack_yaml_contains_ipset_and_webacl():
    ctx = _make_cf_context(waf=CloudFrontWafContext())
    with mock.patch("boto3.client"):
        yaml = CloudFrontWafStack(ctx).yaml
    # IPSet (Scope=CLOUDFRONT, Addresses=[])
    assert "AWS::WAFv2::IPSet" in yaml
    assert "Scope: CLOUDFRONT" in yaml
    assert "Addresses: []" in yaml
    # WebACL (DefaultAction Block + IPSet allow rule)
    assert "AWS::WAFv2::WebACL" in yaml
    assert "DefaultAction:" in yaml
    assert "Block: {}" in yaml
    assert "IPSetReferenceStatement" in yaml
    # Outputs
    assert "WebACLArn" in yaml
    assert "IPSetId" in yaml
    assert "IPSetName" in yaml


def test_waf_stack_yaml_ip_set_id_uses_getatt_not_ref():
    """`AWS::WAFv2::IPSet` の Ref は <Name>|<UUID>|<Scope> の合成文字列を返し、
    WAFv2 API (GetIPSet/UpdateIPSet) は UUID 単体 (36 char) を要求する。
    `pocket waf ip ...` CLI が IPSetId を直接 API に渡す以上、Output は
    必ず Fn::GetAtt IPSet.Id (= UUID) でなければならない。"""
    ctx = _make_cf_context(waf=CloudFrontWafContext())
    with mock.patch("boto3.client"):
        yaml = CloudFrontWafStack(ctx).yaml
    # IPSetId の Output ブロックを抽出して中身を検証
    ipset_id_section = yaml.split("IPSetId:")[1].split("IPSetName:")[0]
    assert "Fn::GetAtt: IPSet.Id" in ipset_id_section
    assert "Ref: IPSet" not in ipset_id_section


def test_waf_stack_yaml_includes_managed_rule_groups():
    ctx = _make_cf_context(
        waf=CloudFrontWafContext(
            managed_rule_groups=[
                "AWSManagedRulesCommonRuleSet",
                "AWSManagedRulesSQLiRuleSet",
            ]
        )
    )
    with mock.patch("boto3.client"):
        yaml = CloudFrontWafStack(ctx).yaml
    assert "AWSManagedRulesCommonRuleSet" in yaml
    assert "AWSManagedRulesSQLiRuleSet" in yaml
    assert "ManagedRuleGroupStatement" in yaml


def test_waf_stack_yaml_no_managed_rule_groups_block_when_empty():
    ctx = _make_cf_context(waf=CloudFrontWafContext(managed_rule_groups=[]))
    with mock.patch("boto3.client"):
        yaml = CloudFrontWafStack(ctx).yaml
    assert "ManagedRuleGroupStatement" not in yaml


def test_waf_stack_yaml_enable_ip_set_false_skips_ipset():
    """enable_ip_set=false: IPSet 自体を作らず、DefaultAction=Allow、
    managed rules で「許可ベース + 怪しいものブロック」構成。"""
    ctx = _make_cf_context(
        waf=CloudFrontWafContext(
            enable_ip_set=False,
            managed_rule_groups=["AWSManagedRulesCommonRuleSet"],
        )
    )
    with mock.patch("boto3.client"):
        yaml = CloudFrontWafStack(ctx).yaml
    # IPSet / ip-allow rule は出ない (Resources セクション内に IPSet なし)
    assert "AWS::WAFv2::IPSet" not in yaml
    assert "IPSetReferenceStatement" not in yaml
    assert "Name: ip-allow" not in yaml
    # DefaultAction は Allow
    assert "Allow: {}" in yaml
    assert "Block: {}" not in yaml
    # managed rule は priority 0 から
    assert "Priority: 0" in yaml
    assert "ManagedRuleGroupStatement" in yaml
    # Outputs から IPSet 系は消える、WebACLArn だけ残る
    assert "WebACLArn" in yaml
    assert "IPSetArn" not in yaml
    assert "IPSetId" not in yaml


# ---------------------------------------------------------------------------
# allow_rules: schema / cross validation / context / template
# ---------------------------------------------------------------------------


def test_waf_allow_rule_accepts_path_only():
    waf = CloudFrontWaf.model_validate({"allow_rules": [{"path": "/api/health"}]})
    assert waf.allow_rules[0].path == "/api/health"
    assert waf.allow_rules[0].header is None


def test_waf_allow_rule_accepts_header_only_and_both():
    waf = CloudFrontWaf.model_validate(
        {
            "allow_rules": [
                {"header": "SMOKE_ALLOW_SECRET"},
                {"path": "/api/health", "header": "SMOKE_ALLOW_SECRET"},
            ]
        }
    )
    assert waf.allow_rules[0].header == "SMOKE_ALLOW_SECRET"
    assert waf.allow_rules[1].path == "/api/health"


def test_waf_allow_rule_rejects_empty_rule():
    with pytest.raises(ValidationError) as exc:
        CloudFrontWaf.model_validate({"allow_rules": [{}]})
    assert "path か header" in str(exc.value)


def test_waf_allow_rule_rejects_relative_path():
    with pytest.raises(ValidationError):
        CloudFrontWaf.model_validate({"allow_rules": [{"path": "api/health"}]})


def test_waf_allow_rule_rejects_mid_wildcard():
    """'*' は末尾 (prefix 一致) のみ。WAFv2 ByteMatch は中間 wildcard 不可。"""
    with pytest.raises(ValidationError):
        CloudFrontWaf.model_validate({"allow_rules": [{"path": "/api/*/health"}]})


def test_waf_allow_rule_rejects_invalid_header_key():
    """header は managed secret のキー名 (EnvStr) なので '-' 等は不可。"""
    with pytest.raises(ValidationError):
        CloudFrontWaf.model_validate({"allow_rules": [{"header": "BAD-KEY"}]})


def _root_settings_dict(*, waf: dict, awscontainer: bool = True) -> dict:
    data: dict = {
        "stage": "dev",
        "general": {
            "region": "ap-northeast-1",
            "project_name": "testprj",
            "stages": ["dev"],
        },
        "s3": {},
        "cloudfront": {
            "web": {
                "routes": [
                    {"is_default": True, "is_spa": True, "origin_path": "/main"}
                ],
                "waf": waf,
            }
        },
    }
    if awscontainer:
        data["container"] = {
            "main": {
                "dockerfile_path": "Dockerfile",
                "handlers": {
                    "wsgi": {
                        "command": "pocket.django.lambda_handlers.wsgi_handler",
                        "apigateway": {},
                    }
                },
            }
        }
    return data


def test_waf_allow_rule_header_requires_awscontainer():
    from pocket import settings

    data = _root_settings_dict(
        waf={"allow_rules": [{"header": "SMOKE_ALLOW_SECRET"}]}, awscontainer=False
    )
    with pytest.raises(ValidationError) as exc:
        settings.Settings.model_validate(data)
    assert "container is required" in str(exc.value)


def test_waf_allow_rule_path_only_works_without_awscontainer():
    from pocket import settings

    data = _root_settings_dict(
        waf={"allow_rules": [{"path": "/api/health"}]}, awscontainer=False
    )
    s = settings.Settings.model_validate(data)
    assert s.cloudfront["web"].waf is not None


def test_waf_allow_rule_header_rejects_declared_secret_collision():
    from pocket import settings

    data = _root_settings_dict(waf={"allow_rules": [{"header": "MY_SECRET"}]})
    data["container"]["main"]["secrets"] = {
        "managed": {"MY_SECRET": {"type": "password"}}
    }
    with pytest.raises(ValidationError) as exc:
        settings.Settings.model_validate(data)
    assert "MY_SECRET" in str(exc.value)


def test_context_injects_waf_allow_secret():
    """header 宣言から managed secret (type=waf_allow_secret) が自動注入される。"""
    from pocket import settings
    from pocket.context import Context

    data = _root_settings_dict(
        waf={"allow_rules": [{"path": "/api/health", "header": "SMOKE_ALLOW_SECRET"}]}
    )
    s = settings.Settings.model_validate(data)
    context = Context.from_settings(s)
    assert context.container["main"] is not None
    assert context.container["main"].shared_secrets is not None
    managed = context.container["main"].shared_secrets.managed
    assert "SMOKE_ALLOW_SECRET" in managed
    assert managed["SMOKE_ALLOW_SECRET"].type == "waf_allow_secret"
    waf_ctx = context.cloudfront["web"].waf
    assert waf_ctx is not None
    assert waf_ctx.header_secret_keys == ["SMOKE_ALLOW_SECRET"]


def test_waf_allow_rule_context_path_match_derivation():
    from pocket.context import CloudFrontWafAllowRuleContext

    exact = CloudFrontWafAllowRuleContext(path="/api/health")
    assert exact.path_positional_constraint == "EXACTLY"
    assert exact.path_search_string == "/api/health"
    prefix = CloudFrontWafAllowRuleContext(path="/api/smoke/*")
    assert prefix.path_positional_constraint == "STARTS_WITH"
    assert prefix.path_search_string == "/api/smoke/"


def _waf_ctx_with_allow_rules() -> CloudFrontWafContext:
    from pocket.context import CloudFrontWafAllowRuleContext

    return CloudFrontWafContext(
        allow_rules=[
            CloudFrontWafAllowRuleContext(path="/api/health"),
            CloudFrontWafAllowRuleContext(
                path="/api/smoke/*", header="SMOKE_ALLOW_SECRET"
            ),
        ],
        managed_rule_groups=["AWSManagedRulesCommonRuleSet"],
    )


def test_waf_stack_yaml_allow_rules_precede_ip_set():
    """allow rules が priority 0.. を取り、ip-allow / managed rules は後ろへずれる。"""
    import yaml as pyyaml

    ctx = _make_cf_context(waf=_waf_ctx_with_allow_rules())
    with mock.patch("boto3.client"):
        rendered = CloudFrontWafStack(
            ctx, allow_secret_values={"SMOKE_ALLOW_SECRET": "secret-value"}
        ).yaml
    rules = pyyaml.safe_load(rendered)["Resources"]["WebACL"]["Properties"]["Rules"]
    by_name = {r["Name"]: r for r in rules}
    assert by_name["allow-0"]["Priority"] == 0
    assert by_name["allow-1"]["Priority"] == 1
    assert by_name["ip-allow"]["Priority"] == 2
    assert by_name["managed-0-AWSManagedRulesCommonRuleSet"]["Priority"] == 3
    # path のみ: UriPath 単体の ByteMatch
    stmt0 = by_name["allow-0"]["Statement"]["ByteMatchStatement"]
    assert stmt0["FieldToMatch"] == {"UriPath": {}}
    assert stmt0["PositionalConstraint"] == "EXACTLY"
    assert stmt0["SearchString"] == "/api/health"
    # path + header: AND で固定ヘッダ名 + secret 値
    and_stmts = by_name["allow-1"]["Statement"]["AndStatement"]["Statements"]
    header_stmt = and_stmts[1]["ByteMatchStatement"]
    assert header_stmt["FieldToMatch"] == {
        "SingleHeader": {"Name": "x-pocket-waf-allow"}
    }
    assert header_stmt["SearchString"] == "secret-value"
    assert and_stmts[0]["ByteMatchStatement"]["PositionalConstraint"] == "STARTS_WITH"


def test_waf_stack_yaml_no_allow_rules_keeps_ip_allow_priority_zero():
    """allow_rules 無しの既存構成でテンプレートが変わらない (回帰)。"""
    ctx = _make_cf_context(waf=CloudFrontWafContext())
    with mock.patch("boto3.client"):
        rendered = CloudFrontWafStack(ctx).yaml
    assert "allow-0" not in rendered
    assert "Priority: 0" in rendered


def test_waf_resource_prepare_deploy_reads_allow_secrets():
    from pocket_cli.resources.cloudfront_waf import CloudFrontWaf as WafResource

    ctx = _make_cf_context(waf=_waf_ctx_with_allow_rules())
    mediator = mock.MagicMock()
    mediator.context.secrets.pocket_store.secrets = {
        "SMOKE_ALLOW_SECRET": "secret-value",
        "OTHER": "x",
    }
    with mock.patch("boto3.client"):
        res = WafResource(ctx)
        res.prepare_deploy(mediator)
        assert res._allow_secret_values == {"SMOKE_ALLOW_SECRET": "secret-value"}
        assert 'SearchString: "secret-value"' in res.stack.yaml


# ---------------------------------------------------------------------------
# CFn template: CloudFront distribution に WebACLId が attach されるか
# ---------------------------------------------------------------------------


def test_cloudfront_distribution_attaches_web_acl_when_waf_present():
    """waf ありの場合、CloudFrontStack が us-east-1 WAF stack の output を
    読みに行き、WebACLId を distribution に埋め込む。"""
    ctx = _make_cf_context(waf=CloudFrontWafContext())
    fake_arn = (
        "arn:aws:wafv2:us-east-1:123456789012:global/webacl/dev-testprj-web-waf/abc"
    )
    with (
        mock.patch("boto3.client"),
        mock.patch.object(
            CloudFrontWafStack,
            "output",
            new_callable=mock.PropertyMock,
            return_value={
                "WebACLArn": fake_arn,
                "IPSetArn": "arn:...",
                "IPSetId": "abc",
                "IPSetName": "dev-testprj-web-waf-allow",
            },
        ),
    ):
        yaml = CloudFrontStack(ctx).yaml
    assert f'WebACLId: "{fake_arn}"' in yaml


def test_cloudfront_distribution_omits_web_acl_when_no_waf():
    """waf 未設定時は WebACLId フィールドが distribution 出力に現れない。

    既存 toml (waf なし) を壊さないことの回帰テスト。
    """
    ctx = _make_cf_context(waf=None)
    with mock.patch("boto3.client"):
        yaml = CloudFrontStack(ctx).yaml
    assert "WebACLId" not in yaml


# ---------------------------------------------------------------------------
# CLI: `pocket waf ip ...`
#
# 実際の AWS 呼び出しは mock するが、click のコマンドツリーが正しく組み立たって
# いる (`pocket waf ip list/add/remove/clear` が存在し、引数を受ける) ことを
# smoke で確認する。
# ---------------------------------------------------------------------------


def test_waf_ip_cli_commands_registered():
    from click.testing import CliRunner
    from pocket_cli.cli.waf_cli import waf

    runner = CliRunner()
    result = runner.invoke(waf, ["ip", "--help"])
    assert result.exit_code == 0, result.output
    for sub in ("list", "add", "remove", "clear"):
        assert sub in result.output


def test_waf_ip_cli_rejects_enable_ip_set_false(monkeypatch):
    """enable_ip_set=false の cloudfront に対して `pocket waf ip ...` を
    叩いたら、IPSet がない旨を明示するエラーで止まる。"""
    from click.testing import CliRunner
    from pocket_cli.cli import waf_cli

    from pocket.context import Context

    cf_ctx = _make_cf_context(
        waf=CloudFrontWafContext(
            enable_ip_set=False,
            managed_rule_groups=["AWSManagedRulesCommonRuleSet"],
        )
    )
    fake_root = mock.MagicMock(spec=Context)
    fake_root.cloudfront = {"web": cf_ctx}
    monkeypatch.setattr(Context, "from_toml", classmethod(lambda cls, stage: fake_root))

    runner = CliRunner()
    result = runner.invoke(
        waf_cli.waf, ["ip", "list", "--stage", "dev", "--name", "web"]
    )
    assert result.exit_code != 0
    assert "enable_ip_set" in result.output


def test_waf_ip_add_self_uses_checkip_then_update_ip_set(monkeypatch):
    """`add self` が IP 検出 → get_ip_set → update_ip_set を呼ぶ。"""
    from click.testing import CliRunner
    from pocket_cli.cli import waf_cli

    cf_ctx = _make_cf_context(waf=CloudFrontWafContext())

    monkeypatch.setattr(waf_cli, "_get_cf_context", lambda stage, name: cf_ctx)
    monkeypatch.setattr(
        waf_cli, "_get_ip_set_meta", lambda ctx: ("dev-testprj-web-waf-allow", "abc")
    )
    monkeypatch.setattr(waf_cli, "_detect_self_ipv4", lambda: "198.51.100.7/32")

    fake_client = mock.MagicMock()
    fake_client.get_ip_set.return_value = {
        "IPSet": {"Addresses": []},
        "LockToken": "tok",
    }
    monkeypatch.setattr(waf_cli, "_wafv2_client", lambda: fake_client)

    runner = CliRunner()
    result = runner.invoke(
        waf_cli.waf, ["ip", "add", "self", "--stage", "dev", "--name", "web"]
    )
    assert result.exit_code == 0, result.output
    fake_client.update_ip_set.assert_called_once_with(
        Name="dev-testprj-web-waf-allow",
        Scope="CLOUDFRONT",
        Id="abc",
        Addresses=["198.51.100.7/32"],
        LockToken="tok",
    )


# ---------------------------------------------------------------------------
# rate_limits (KN1870)
# ---------------------------------------------------------------------------


def test_waf_rate_limit_accepts_minimal_and_full():
    waf = CloudFrontWaf.model_validate({"rate_limits": [{"limit": 1000}]})
    rule = waf.rate_limits[0]
    assert (rule.limit, rule.window_sec, rule.action, rule.path) == (
        1000,
        300,
        "block",
        None,
    )
    waf = CloudFrontWaf.model_validate(
        {
            "rate_limits": [
                {
                    "limit": 30,
                    "window_sec": 60,
                    "action": "count",
                    "path": "/api/auth/*",
                }
            ]
        }
    )
    assert waf.rate_limits[0].path == "/api/auth/*"


@pytest.mark.parametrize(
    "rule, message",
    [
        ({"limit": 9}, "greater than or equal to 10"),
        ({"limit": 100, "window_sec": 90}, "window_sec は 60 / 120 / 300 / 600"),
        ({"limit": 100, "action": "captcha"}, "action"),
        ({"limit": 100, "path": "api/*"}, "path は '/' で始めて"),
        ({"limit": 100, "path": "/api/*/x"}, "'*' は末尾"),
        ({"limit": 100, "path": '/api/"x'}, "使えない文字"),
        ({"limit": 100, "unknown": 1}, "unknown"),
    ],
)
def test_waf_rate_limit_rejects_invalid(rule, message):
    with pytest.raises(ValidationError, match=message):
        CloudFrontWaf.model_validate({"rate_limits": [rule]})


def test_waf_allow_rule_path_rejects_unsafe_chars():
    """allow_rules の path も同じ文字種制限 (YAML への素の埋め込みを守る)。"""
    from pocket.settings import CloudFrontWafAllowRule

    with pytest.raises(ValidationError, match="使えない文字"):
        CloudFrontWafAllowRule.model_validate({"path": "/api/back\\slash"})
    CloudFrontWafAllowRule.model_validate(
        {"path": "/api/v1/items-(a)_b.c~!$&'+,;=:@%*"}
    )


def test_waf_rate_limits_only_is_valid_without_ip_set():
    """IP allowlist 無し + block の rate_limits だけの WebACL を宣言できる。"""
    waf = CloudFrontWaf.model_validate(
        {"enable_ip_set": False, "rate_limits": [{"limit": 1000}]}
    )
    assert waf.managed_rule_groups == []


def test_waf_count_only_rate_limits_rejected_without_other_blocking_rule():
    """count だけでは何も遮断しないので pass-through として拒否する。"""
    with pytest.raises(ValidationError, match='action = "block"'):
        CloudFrontWaf.model_validate(
            {
                "enable_ip_set": False,
                "rate_limits": [{"limit": 1000, "action": "count"}],
            }
        )
    # managed rules があれば count だけでもよい
    CloudFrontWaf.model_validate(
        {
            "enable_ip_set": False,
            "managed_rule_groups": ["AWSManagedRulesCommonRuleSet"],
            "rate_limits": [{"limit": 1000, "action": "count"}],
        }
    )


def test_waf_rate_limits_capped_at_aws_quota():
    with pytest.raises(ValidationError, match="10 件まで"):
        CloudFrontWaf.model_validate({"rate_limits": [{"limit": 100}] * 11})


def test_context_maps_rate_limits():
    from pocket.context import Context
    from pocket.settings import Settings

    data = _root_settings_dict(
        waf={
            "enable_ip_set": False,
            "rate_limits": [
                {"limit": 1000},
                {"limit": 30, "window_sec": 60, "action": "count", "path": "/a/*"},
            ],
        }
    )
    context = Context.from_settings(Settings.model_validate(data))
    waf_ctx = context.cloudfront["web"].waf
    assert waf_ctx is not None
    assert [r.limit for r in waf_ctx.rate_limits] == [1000, 30]
    assert waf_ctx.rate_limits[1].action == "count"
    assert waf_ctx.rate_limits[1].path_positional_constraint == "STARTS_WITH"
    assert waf_ctx.rate_limits[1].path_search_string == "/a/"


def _waf_ctx_with_rate_limits(**kwargs) -> CloudFrontWafContext:
    from pocket.context import (
        CloudFrontWafAllowRuleContext,
        CloudFrontWafRateLimitContext,
    )

    return CloudFrontWafContext(
        rate_limits=[
            CloudFrontWafRateLimitContext(
                limit=30, window_sec=60, action="block", path="/api/auth/*"
            ),
            CloudFrontWafRateLimitContext(limit=1000, window_sec=300, action="count"),
        ],
        allow_rules=[CloudFrontWafAllowRuleContext(path="/api/health")],
        managed_rule_groups=["AWSManagedRulesCommonRuleSet"],
        **kwargs,
    )


def _render_rules(ctx: CloudFrontContext) -> dict:
    import yaml as pyyaml

    with mock.patch("boto3.client"):
        rendered = CloudFrontWafStack(ctx).yaml
    rules = pyyaml.safe_load(rendered)["Resources"]["WebACL"]["Properties"]["Rules"]
    return {r["Name"]: r for r in rules}


def test_waf_stack_yaml_rate_limits_precede_everything():
    """rate rule が priority 0.. を取り、allow / ip-allow / managed は後ろへずれる。"""
    by_name = _render_rules(_make_cf_context(waf=_waf_ctx_with_rate_limits()))
    assert by_name["ratelimit-0"]["Priority"] == 0
    assert by_name["ratelimit-1"]["Priority"] == 1
    assert by_name["allow-0"]["Priority"] == 2
    assert by_name["ip-allow"]["Priority"] == 3
    assert by_name["managed-0-AWSManagedRulesCommonRuleSet"]["Priority"] == 4
    # block は 429 の CustomResponse、path は ScopeDown の prefix 一致
    rule0 = by_name["ratelimit-0"]
    assert rule0["Action"] == {"Block": {"CustomResponse": {"ResponseCode": 429}}}
    stmt0 = rule0["Statement"]["RateBasedStatement"]
    assert (
        stmt0["Limit"],
        stmt0["EvaluationWindowSec"],
        stmt0["AggregateKeyType"],
    ) == (
        30,
        60,
        "IP",
    )
    scope = stmt0["ScopeDownStatement"]["ByteMatchStatement"]
    assert scope["FieldToMatch"] == {"UriPath": {}}
    assert scope["PositionalConstraint"] == "STARTS_WITH"
    assert scope["SearchString"] == "/api/auth/"
    # count は Count、path 無しは ScopeDown 無し
    rule1 = by_name["ratelimit-1"]
    assert rule1["Action"] == {"Count": {}}
    assert "ScopeDownStatement" not in rule1["Statement"]["RateBasedStatement"]
    assert rule1["VisibilityConfig"]["MetricName"] == "dev-testprj-web-waf-ratelimit-1"


def test_waf_stack_yaml_rate_limits_without_ip_set():
    """enable_ip_set=false + rate_limits のみでは rule が rate だけになる。"""
    from pocket.context import CloudFrontWafRateLimitContext

    ctx = _make_cf_context(
        waf=CloudFrontWafContext(
            enable_ip_set=False,
            rate_limits=[
                CloudFrontWafRateLimitContext(
                    limit=1000, window_sec=300, action="block"
                )
            ],
        )
    )
    by_name = _render_rules(ctx)
    assert list(by_name) == ["ratelimit-0"]


@pytest.mark.parametrize("path", ["/api/v1/items-(a)_b.c~!$&'+,;=:@%", "/x/*", "/"])
def test_waf_stack_yaml_path_round_trips_through_yaml(path):
    """許可した文字種の path が YAML を経由しても値が変わらない。"""
    from pocket.context import CloudFrontWafRateLimitContext

    ctx = _make_cf_context(
        waf=CloudFrontWafContext(
            rate_limits=[
                CloudFrontWafRateLimitContext(
                    limit=10, window_sec=60, action="block", path=path
                )
            ]
        )
    )
    scope = _render_rules(ctx)["ratelimit-0"]["Statement"]["RateBasedStatement"][
        "ScopeDownStatement"
    ]["ByteMatchStatement"]
    expected = path[:-1] if path.endswith("*") else path
    assert scope["SearchString"] == expected


def test_waf_stack_yaml_without_rate_limits_is_unchanged():
    """rate_limits 未宣言なら rule も priority も従来どおり (既存 deploy に差分なし)"""
    by_name = _render_rules(_make_cf_context(waf=_waf_ctx_with_allow_rules()))
    assert not any(n.startswith("ratelimit-") for n in by_name)
    assert by_name["allow-0"]["Priority"] == 0
    assert by_name["ip-allow"]["Priority"] == 2


def test_waf_resource_echoes_rate_limits():
    from pocket_cli.resources.cloudfront_waf import CloudFrontWaf as WafResource

    ctx = _make_cf_context(waf=_waf_ctx_with_rate_limits())
    with mock.patch("boto3.client"):
        resource = WafResource(ctx)
    lines: list[str] = []
    with mock.patch(
        "pocket_cli.resources.cloudfront_waf.echo.info", side_effect=lines.append
    ):
        resource._echo_rate_limits()
    assert any("全ルールの先頭で評価" in line for line in lines)
    assert "  ratelimit-0: 30 req / 60 秒, path=/api/auth/*, action=block" in lines
    assert (
        "  ratelimit-1: 1000 req / 300 秒, path=(全リクエスト), action=count" in lines
    )


def test_waf_ratelimit_cli_registered():
    from click.testing import CliRunner
    from pocket_cli.cli.waf_cli import waf

    result = CliRunner().invoke(waf, ["ratelimit", "--help"])
    assert result.exit_code == 0, result.output
    assert "list" in result.output


def test_waf_ratelimit_list_reads_deployed_rules_and_managed_keys(monkeypatch):
    """deploy 済み WebACL の rate rule を読み、rule ごとに managed keys を表示する。"""
    from click.testing import CliRunner
    from pocket_cli.cli import waf_cli

    cf_ctx = _make_cf_context(waf=_waf_ctx_with_rate_limits())
    monkeypatch.setattr(waf_cli, "_get_waf_cf_context", lambda stage, name: cf_ctx)
    monkeypatch.setattr(
        waf_cli, "_get_web_acl_meta", lambda ctx: ("dev-testprj-web-waf", "acl-id")
    )
    fake_client = mock.MagicMock()
    # ローカル宣言と違う順序・内容を返し、表示が deploy 済みの実態に従うことを見る
    fake_client.get_web_acl.return_value = {
        "WebACL": {
            "Rules": [
                {
                    "Name": "allow-0",
                    "Priority": 1,
                    "Statement": {"ByteMatchStatement": {}},
                    "Action": {"Allow": {}},
                },
                {
                    "Name": "ratelimit-0",
                    "Priority": 0,
                    "Statement": {
                        "RateBasedStatement": {
                            "Limit": 500,
                            "EvaluationWindowSec": 120,
                            "AggregateKeyType": "IP",
                            "ScopeDownStatement": {
                                "ByteMatchStatement": {
                                    "SearchString": b"/api/",
                                    "PositionalConstraint": "STARTS_WITH",
                                }
                            },
                        }
                    },
                    "Action": {"Count": {}},
                },
            ]
        }
    }
    fake_client.get_rate_based_statement_managed_keys.return_value = {
        "ManagedKeysIPV4": {"Addresses": ["198.51.100.7/32"]},
        "ManagedKeysIPV6": {"Addresses": []},
    }
    monkeypatch.setattr(waf_cli, "_wafv2_client", lambda: fake_client)

    result = CliRunner().invoke(
        waf_cli.waf, ["ratelimit", "list", "--stage", "dev", "--name", "web"]
    )
    assert result.exit_code == 0, result.output
    fake_client.get_web_acl.assert_called_once_with(
        Name="dev-testprj-web-waf", Scope="CLOUDFRONT", Id="acl-id"
    )
    fake_client.get_rate_based_statement_managed_keys.assert_called_once_with(
        Scope="CLOUDFRONT",
        WebACLName="dev-testprj-web-waf",
        WebACLId="acl-id",
        RuleName="ratelimit-0",
    )
    assert "ratelimit-0: 500 req / 120 秒, path=/api/*, action=count" in result.output
    assert "198.51.100.7/32" in result.output
    assert "ratelimit-1" not in result.output


def test_waf_ratelimit_list_warns_when_no_rate_rules_deployed(monkeypatch):
    from click.testing import CliRunner
    from pocket_cli.cli import waf_cli

    cf_ctx = _make_cf_context(waf=CloudFrontWafContext())
    monkeypatch.setattr(waf_cli, "_get_waf_cf_context", lambda stage, name: cf_ctx)
    monkeypatch.setattr(waf_cli, "_get_web_acl_meta", lambda ctx: ("acl", "id"))
    fake_client = mock.MagicMock()
    fake_client.get_web_acl.return_value = {"WebACL": {"Rules": []}}
    monkeypatch.setattr(waf_cli, "_wafv2_client", lambda: fake_client)

    result = CliRunner().invoke(
        waf_cli.waf, ["ratelimit", "list", "--stage", "dev", "--name", "web"]
    )
    assert result.exit_code == 0, result.output
    assert "rate rule がありません" in result.output
    fake_client.get_rate_based_statement_managed_keys.assert_not_called()


def test_web_acl_meta_parses_arn():
    from pocket_cli.cli import waf_cli

    cf_ctx = _make_cf_context(waf=CloudFrontWafContext())
    fake_stack = mock.MagicMock()
    arn = "arn:aws:wafv2:us-east-1:123456789012:global/webacl/dev-testprj-web-waf/uid"
    fake_stack.output = {"WebACLArn": arn}
    with mock.patch.object(waf_cli, "CloudFrontWafStack", return_value=fake_stack):
        assert waf_cli._get_web_acl_meta(cf_ctx) == ("dev-testprj-web-waf", "uid")
