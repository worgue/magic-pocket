import pytest

from pocket import settings
from pocket.utils import get_hosted_zone_id_from_domain, get_hosted_zones


@pytest.fixture
def base_settings():
    return settings.Settings.model_validate(
        {
            "stage": "test",
            "general": {
                "region": "ap-southeast-1",
                "project_name": "testprj",
                "stages": ["dev", "prod"],
            },
        }
    )


@pytest.fixture
def aws_settings():
    management_name = "pocket.django.lambda_handlers.management_command_handler"
    return settings.AwsContainer.model_validate(
        {
            "dockerfile_path": "Dockerfile",
            "handlers": {
                "wsgi": {"command": "pocket.django.lambda_handlers.wsgi_handler"},
                "management": {"command": management_name},
            },
        }
    )


@pytest.fixture(autouse=True)
def cleaned_cache():
    get_hosted_zones.cache_clear()
    get_hosted_zone_id_from_domain.cache_clear()
    return True
