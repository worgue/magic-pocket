"""get_hosted_zone_id_from_domain の照合ロジックのテスト。

ラベル境界を無視した substring 判定だと、example.com zone が
badexample.com (無関係な registrable domain) に誤マッチし、
誤った zone への ACM 検証 / Alias レコード作成につながる。
"""

import boto3
import pytest
from moto import mock_aws

from pocket.utils import get_hosted_zone_id_from_domain


def _create_zone(name: str) -> str:
    res = boto3.client("route53").create_hosted_zone(
        Name=name, CallerReference="test-%s" % name
    )
    return res["HostedZone"]["Id"][len("/hostedzone/") :]


@mock_aws
def test_exact_domain_matches():
    zone_id = _create_zone("example.com.")
    assert get_hosted_zone_id_from_domain("example.com") == zone_id


@mock_aws
def test_subdomain_matches():
    zone_id = _create_zone("example.com.")
    assert get_hosted_zone_id_from_domain("api.example.com") == zone_id


@mock_aws
def test_unrelated_domain_with_suffix_substring_does_not_match():
    """badexample.com は example.com zone にマッチしないこと (label 境界)"""
    _create_zone("example.com.")
    with pytest.raises(Exception, match="No route53 hosted zone"):
        get_hosted_zone_id_from_domain("badexample.com")


@mock_aws
def test_longest_zone_wins_for_nested_zones():
    """親子 zone を両方保有する場合は最長一致 (子 zone) を選ぶこと"""
    _create_zone("example.com.")
    sub_id = _create_zone("dev.example.com.")
    assert get_hosted_zone_id_from_domain("api.dev.example.com") == sub_id
