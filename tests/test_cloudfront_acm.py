"""cloudfront_acm.yaml テンプレートの レンダリング検証。

redirect_from は専用 cert ではなくメイン cert の SubjectAlternativeNames に載る
(301 は CloudFront Function 方式)。ここではメイン cert に SAN と各ドメインの
DomainValidationOptions が正しく載ることを検証する。
"""

import yaml as yaml_lib
from pocket_cli.resources.aws.cloudformation import AcmStack

from pocket.context import CloudFrontContext, RedirectFromContext, RouteContext


def _make_context(*, domain: str, redirect_from: list[RedirectFromContext]):
    return CloudFrontContext(
        name="web",
        region="ap-northeast-1",
        s3_region="ap-northeast-1",
        stage="dev",
        domain=domain,
        hosted_zone_id_override="ZPARENT0000000",
        slug="dev-testprj-web",
        bucket_name="dev-testprj-bucket",
        resource_prefix="dev-testprj-",
        redirect_from=redirect_from,
        routes=[RouteContext(is_default=True, origin_path="/app")],
    )


def _cert_props(ctx) -> dict:
    doc = yaml_lib.safe_load(AcmStack(ctx).yaml)
    return doc["Resources"]["Certificate"]["Properties"]


def test_acm_main_cert_uses_parent_hosted_zone_id():
    props = _cert_props(_make_context(domain="app.example.com", redirect_from=[]))
    dvo = {d["DomainName"]: d["HostedZoneId"] for d in props["DomainValidationOptions"]}
    assert dvo == {"app.example.com": "ZPARENT0000000"}
    # redirect_from が無ければ SAN は生えない
    assert "SubjectAlternativeNames" not in props


def test_acm_redirect_domain_is_san_on_main_cert():
    """redirect_from ドメインはメイン cert の SAN として載る (専用 cert を作らない)。"""
    rf = RedirectFromContext(
        domain="alias.example.org",
        hosted_zone_id_override="ZRF0000000000",
    )
    ctx = _make_context(domain="app.example.com", redirect_from=[rf])
    yaml = AcmStack(ctx).yaml
    props = _cert_props(ctx)
    assert props["SubjectAlternativeNames"] == ["alias.example.org"]
    # 専用 cert リソース / 出力は存在しない
    assert "CertificateAliasExampleOrg" not in yaml
    assert "CertificateArnAliasExampleOrg" not in yaml


def test_acm_redirect_domain_validation_uses_rf_hosted_zone_id_not_parent():
    """SAN の DomainValidationOptions は rf 自身の hosted_zone_id を使う。

    親と redirect 先で異なる Hosted Zone のとき、各ドメインの検証レコードは
    それぞれのゾーンに作られる必要がある。
    """
    rf = RedirectFromContext(
        domain="alias.example.org",
        hosted_zone_id_override="ZRF0000000000",
    )
    ctx = _make_context(domain="app.example.com", redirect_from=[rf])
    props = _cert_props(ctx)
    dvo = {d["DomainName"]: d["HostedZoneId"] for d in props["DomainValidationOptions"]}
    assert dvo == {
        "app.example.com": "ZPARENT0000000",
        "alias.example.org": "ZRF0000000000",
    }


def test_acm_redirect_domain_with_same_zone_as_parent():
    rf = RedirectFromContext(
        domain="alias.example.com",
        hosted_zone_id_override="ZPARENT0000000",
    )
    ctx = _make_context(domain="app.example.com", redirect_from=[rf])
    props = _cert_props(ctx)
    dvo = [d["HostedZoneId"] for d in props["DomainValidationOptions"]]
    assert dvo == ["ZPARENT0000000", "ZPARENT0000000"]


def test_acm_multiple_redirect_domains_all_san():
    ctx = _make_context(
        domain="www.example.com",
        redirect_from=[
            RedirectFromContext(domain="example.com", hosted_zone_id_override="Z1"),
            RedirectFromContext(domain="www.example.net", hosted_zone_id_override="Z2"),
        ],
    )
    props = _cert_props(ctx)
    assert props["SubjectAlternativeNames"] == ["example.com", "www.example.net"]
