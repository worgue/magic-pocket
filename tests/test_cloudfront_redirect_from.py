"""redirect_from の CloudFront Function 方式 (cloudfront.yaml) レンダリング検証。

redirect_from ドメインは:
  - メイン distribution の Alias になる (専用 distribution を作らない)
  - 非 canonical host → canonical domain の 301 を viewer-request Function で返す
  - S3 website バケットを一切使わない
"""

import pytest
import yaml as yaml_lib
from pocket_cli.resources.aws.cloudformation import CloudFrontStack

from pocket.context import CloudFrontContext, RedirectFromContext, RouteContext


def _stack(*, routes, redirect_from, **overrides):
    ctx = CloudFrontContext(
        name="web",
        region="ap-northeast-1",
        s3_region="ap-northeast-1",
        stage="dev",
        domain=overrides.get("domain", "www.example.com"),
        hosted_zone_id_override="ZPARENT",
        slug="dev-testprj-web",
        bucket_name="dev-testprj-bucket",
        resource_prefix="dev-testprj-",
        redirect_from=redirect_from,
        routes=routes,
        **{k: v for k, v in overrides.items() if k != "domain"},
    )
    stack = CloudFrontStack(ctx)
    stack._resolve_acm_arn = lambda: "arn:aws:acm:us-east-1:0:certificate/x"
    stack._resolve_waf_arn = lambda: None
    return stack


def _rf(domain="foo-bar.example.com"):
    return [RedirectFromContext(domain=domain, hosted_zone_id_override="ZRF")]


def _doc(stack):
    return yaml_lib.safe_load(stack.yaml)


def test_redirect_domain_added_to_main_distribution_aliases():
    doc = _doc(_stack(routes=[RouteContext(is_default=True)], redirect_from=_rf()))
    aliases = doc["Resources"]["CloudFrontDistribution"]["Properties"][
        "DistributionConfig"
    ]["Aliases"]
    assert aliases == ["www.example.com", "foo-bar.example.com"]


def test_no_legacy_dedicated_distribution_or_bucket():
    """専用 distribution / S3 website origin を作らない。"""
    stack = _stack(routes=[RouteContext(is_default=True)], redirect_from=_rf())
    yaml = stack.yaml
    res = _doc(stack)["Resources"]
    assert not any(k.startswith("CloudFrontDistributionFoo") for k in res)
    assert "s3-website" not in yaml
    assert "RedirectAllRequestsTo" not in yaml


def test_dns_record_points_to_main_distribution_with_rf_zone():
    stack = _stack(routes=[RouteContext(is_default=True)], redirect_from=_rf())
    res = _doc(stack)["Resources"]
    # 論理名は非英数字が除去された CamelCase
    assert "DNSRecordFooBarExampleCom" in res
    props = res["DNSRecordFooBarExampleCom"]["Properties"]
    assert props["HostedZoneId"] == "ZRF"
    assert props["Name"] == "foo-bar.example.com"
    assert props["AliasTarget"]["DNSName"] == {
        "Fn::GetAtt": "CloudFrontDistribution.DomainName"
    }


def test_host_redirect_function_present_with_canonical_domain():
    stack = _stack(routes=[RouteContext(is_default=True)], redirect_from=_rf())
    res = _doc(stack)["Resources"]
    assert "HostRedirectFunction" in res
    code = res["HostRedirectFunction"]["Properties"]["FunctionCode"]
    assert "https://www.example.com" in code
    assert "301" in code


def test_plain_s3_default_behavior_gets_host_redirect_association():
    stack = _stack(routes=[RouteContext(is_default=True)], redirect_from=_rf())
    dcb = _doc(stack)["Resources"]["CloudFrontDistribution"]["Properties"][
        "DistributionConfig"
    ]["DefaultCacheBehavior"]
    assert "HostRedirectFunction" in yaml_lib.dump(dcb["FunctionAssociations"])


def test_managed_asset_behavior_gets_host_redirect_association(tmp_path):
    (tmp_path / "default").mkdir()
    (tmp_path / "default" / "robots.txt").write_text("User-agent: *\n")
    stack = _stack(
        routes=[RouteContext(is_default=True)],
        redirect_from=_rf(),
        managed_assets=str(tmp_path),
    )
    behaviors = _doc(stack)["Resources"]["CloudFrontDistribution"]["Properties"][
        "DistributionConfig"
    ]["CacheBehaviors"]
    robots = [b for b in behaviors if b["PathPattern"] == "/robots.txt"]
    assert robots, behaviors
    assert "HostRedirectFunction" in yaml_lib.dump(robots[0]["FunctionAssociations"])


def test_lambda_default_behavior_uses_apihost_with_injected_prelude():
    """default が lambda のとき、default behavior は ApiHostFunction のままで、
    その関数コードに redirect prelude が注入される (関数を増やさない)。"""
    stack = _stack(
        routes=[RouteContext(is_default=True, type="lambda", handler="api")],
        redirect_from=_rf(),
        api_origins={"api": "ExportApiOrigin"},
    )
    res = _doc(stack)["Resources"]
    dcb = res["CloudFrontDistribution"]["Properties"]["DistributionConfig"][
        "DefaultCacheBehavior"
    ]
    assoc = yaml_lib.dump(dcb["FunctionAssociations"])
    assert "ApiHostFunction" in assoc
    assert "HostRedirectFunction" not in assoc
    api_code = res["ApiHostFunction"]["Properties"]["FunctionCode"]
    assert "https://www.example.com" in api_code  # prelude injected
    assert "x-forwarded-host" in api_code  # original logic kept


def test_no_redirect_from_leaves_everything_off():
    stack = _stack(routes=[RouteContext(is_default=True)], redirect_from=[])
    yaml = stack.yaml
    res = _doc(stack)["Resources"]
    assert "HostRedirectFunction" not in res
    assert "301" not in yaml
    aliases = res["CloudFrontDistribution"]["Properties"]["DistributionConfig"][
        "Aliases"
    ]
    assert aliases == ["www.example.com"]
    # DNS レコードは canonical の 1 本のみ
    assert [k for k in res if k.startswith("DNSRecord")] == ["DNSRecord"]


def test_redirect_prelude_returns_valid_yaml_for_all_function_kinds():
    """SPA / spa_auth / deploy_hash / api / plain の全 Function に prelude 注入して
    YAML が壊れないこと (インデント崩れ検出)。"""
    stack = _stack(
        routes=[
            RouteContext(is_default=True, is_spa=True),
            RouteContext(
                path_pattern="/auth", is_spa=True, require_token=True, login_path="/l"
            ),
            RouteContext(path_pattern="/v", versioning="deploy_hash"),
            RouteContext(path_pattern="/api", type="lambda", handler="api"),
            RouteContext(path_pattern="/plain"),
        ],
        redirect_from=_rf(),
        deploy_hash="abc1234",
        api_origins={"api": "ExportApiOrigin"},
    )
    doc = _doc(stack)  # raises if YAML invalid
    funcs = {
        k
        for k, v in doc["Resources"].items()
        if v["Type"] == "AWS::CloudFront::Function"
    }
    # 全 viewer-request Function に prelude が入る
    for name in funcs:
        fc = doc["Resources"][name]["Properties"]["FunctionCode"]
        body = fc["Fn::Sub"][0] if isinstance(fc, dict) else fc
        assert "https://www.example.com" in body, name


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
