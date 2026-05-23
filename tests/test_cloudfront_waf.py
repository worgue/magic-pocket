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
