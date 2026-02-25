from pathlib import Path

import pytest

from pocket import settings
from pocket.runtime import get_context
from pocket.utils import get_hosted_zone_id_from_domain, get_hosted_zones


@pytest.fixture
def use_toml(monkeypatch):
    """テスト用の pocket.toml パスを設定するフィクスチャ"""

    def _use(path: str):
        p = Path(path)
        monkeypatch.setattr("pocket.settings.get_toml_path", lambda: p)
        monkeypatch.setattr("pocket.general_settings.get_toml_path", lambda: p)

    return _use


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
    get_context.cache_clear()
    return True
