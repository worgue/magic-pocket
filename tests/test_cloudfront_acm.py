"""cloudfront_acm.yaml テンプレートの レンダリング検証。"""

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


def test_acm_main_cert_uses_parent_hosted_zone_id():
    ctx = _make_context(domain="app.example.com", redirect_from=[])
    yaml = AcmStack(ctx).yaml
    # 親 cert (Certificate) は親の hosted_zone_id (override) を使う
    assert "ZPARENT0000000" in yaml


def test_acm_redirect_cert_uses_rf_hosted_zone_id_not_parent():
    """redirect_from の cert は rf 自身の hosted_zone_id を参照する。

    親と redirect 先で異なる Hosted Zone を指定したとき、redirect cert の
    DomainValidationOptions.HostedZoneId は redirect 先のゾーンであるべき。
    """
    rf = RedirectFromContext(
        domain="alias.example.org",
        hosted_zone_id_override="ZRF0000000000",
    )
    ctx = _make_context(domain="app.example.com", redirect_from=[rf])
    yaml = AcmStack(ctx).yaml
    # 親 cert は親の zone
    assert "ZPARENT0000000" in yaml
    # redirect cert は rf の zone (バグ修正前は ZPARENT を参照していた)
    assert "ZRF0000000000" in yaml
    # redirect cert ブロックで rf の zone のみが現れる
    redirect_section = yaml.split('"CertificateAliasExampleOrg"')[1].split("Tags:")[0]
    assert "ZRF0000000000" in redirect_section
    assert "ZPARENT0000000" not in redirect_section


def test_acm_redirect_cert_with_same_zone_as_parent():
    # rf が親と同じゾーン上のドメインでも、rf 自身の hosted_zone_id が引かれる
    rf = RedirectFromContext(
        domain="alias.example.com",
        hosted_zone_id_override="ZPARENT0000000",
    )
    ctx = _make_context(domain="app.example.com", redirect_from=[rf])
    yaml = AcmStack(ctx).yaml
    # 親 / redirect どちらの cert ブロックでも ZPARENT が現れる
    assert yaml.count("ZPARENT0000000") >= 2


def test_acm_redirect_cert_logical_name_strips_non_alphanumeric():
    """ハイフン付き redirect_from domain の cert 論理 ID が英数字のみになる。

    CFn 論理 ID は英数字のみ。yaml_key が非英数字 (ハイフン等) を残すと
    "Resource name ... is non alphanumeric" で UpdateStack が失敗する
    (apex→www の定番 redirect でハイフン付きドメインを使うと全滅していた)。
    """
    rf = RedirectFromContext(
        domain="foo-bar-baz.example.com",
        hosted_zone_id_override="ZRF0000000000",
    )
    ctx = _make_context(domain="www.example.com", redirect_from=[rf])
    yaml = AcmStack(ctx).yaml
    # 非英数字を除去した CamelCase 論理名で cert ブロックが生成される
    assert '"CertificateFooBarBazExampleCom"' in yaml
    assert '"CertificateArnFooBarBazExampleCom"' in yaml
    # ハイフンを残した壊れた論理名は現れない
    assert "Foo-bar-baz" not in yaml
