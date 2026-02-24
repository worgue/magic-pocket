import boto3
from moto import mock_aws

from pocket.context import Context
from pocket.settings import Settings


def test_settings_from_toml():
    settings = Settings.from_toml(stage="dev", path="tests/data/toml/default.toml")
    assert settings.project_name == "testprj"


@mock_aws
def test_context():
    res = boto3.client("route53").create_hosted_zone(
        Name="project.com.", CallerReference="test"
    )
    hosted_zone_id = res["HostedZone"]["Id"][len("/hostedzone/") :]
    context = Context.from_toml(stage="dev", path="tests/data/toml/default.toml")
    assert context.project_name == "testprj"
    assert context.awscontainer
    handlers = context.awscontainer.handlers
    assert handlers["wsgi"].apigateway
    assert handlers["wsgi"].apigateway.hosted_zone_id == hosted_zone_id
    assert handlers["sqsmanagement"].sqs
    assert handlers["sqsmanagement"].sqs.name == "dev-testprj-pocket-sqsmanagement"
    # CloudFront は S3 バケットを共有
    assert context.cloudfront
    assert context.s3
    assert context.cloudfront.bucket_name == context.s3.bucket_name
    assert context.cloudfront.origin_prefix == "/spa"


@mock_aws
def test_yaml():
    res = boto3.client("route53").create_hosted_zone(
        Name="project.com.", CallerReference="test"
    )
    hosted_zone_id = res["HostedZone"]["Id"][len("/hostedzone/") :]
    context = Context.from_toml(stage="dev", path="tests/data/toml/default.toml")
    assert context.awscontainer
    handlers = context.awscontainer.handlers
    assert handlers["wsgi"].apigateway
    assert handlers["wsgi"].apigateway.hosted_zone_id == hosted_zone_id
