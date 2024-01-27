import boto3
from moto import mock_route53

from pocket.context import Context
from pocket.settings import Settings


def test_settings_from_toml():
    settings = Settings.from_toml(stage="dev", path="tests/data/toml/default.toml")
    assert settings.project_name == "testprj"


@mock_route53
def test_context():
    res = boto3.client("route53").create_hosted_zone(
        Name="project.com.", CallerReference="test"
    )
    hosted_zone_id = res["HostedZone"]["Id"][len("/hostedzone/") :]
    context = Context.from_toml(stage="dev", path="tests/data/toml/default.toml")
    assert context.project_name == "testprj"
    handlers = context.awscontainer.handlers
    assert handlers["wsgi"].apigateway.hosted_zone_id == hosted_zone_id
    assert handlers["sqsmanagement"].sqs.name == "dev-testprj-sqsmanagement"
