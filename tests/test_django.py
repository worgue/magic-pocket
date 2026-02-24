import pytest

from pocket import settings
from pocket.context import Context
from pocket.django.context import DjangoStorageContext
from pocket.django.django_cli import _get_management_command_handler
from pocket.django.utils import get_caches, get_storages


def test_manage_cli(base_settings, aws_settings):
    s = base_settings
    with pytest.raises(Exception, match="awscontainer is not configured .*"):
        _get_management_command_handler(Context.from_settings(s))
    s.awscontainer = aws_settings
    handler = _get_management_command_handler(Context.from_settings(s))
    assert handler
    m = s.awscontainer.handlers.pop("management")
    with pytest.raises(Exception, match="Add management command handler for this .*"):
        _get_management_command_handler(Context.from_settings(s))
    s.awscontainer.handlers["m1"] = m
    s.awscontainer.handlers["m2"] = m
    with pytest.raises(Exception, match="Only one management command handler is .*"):
        settings.Settings.model_validate(s.model_dump())


def test_storages():
    toml_path = "tests/data/toml/default.toml"
    context = Context.from_toml(stage="dev", path=toml_path)
    assert context.awscontainer and context.awscontainer.django
    assert context.awscontainer.django.storages == {
        "default": DjangoStorageContext(
            store="s3", location="media", static=False, manifest=False
        ),
        "staticfiles": DjangoStorageContext(
            store="s3", location="static", static=True, manifest=True
        ),
    }
    storages = get_storages(stage="dev", path=toml_path)
    assert storages == {
        "default": {
            "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
            "OPTIONS": {"bucket_name": "dev-testprj-pocket", "location": "media"},
        },
        "staticfiles": {
            "BACKEND": "storages.backends.s3boto3.S3ManifestStaticStorage",
            "OPTIONS": {"bucket_name": "dev-testprj-pocket", "location": "static"},
        },
    }


def test_cache():
    toml_path = "tests/data/toml/default.toml"
    context = Context.from_toml(stage="dev", path=toml_path)
    assert (
        context.awscontainer
        and context.awscontainer.django
        and context.awscontainer.django.caches["default"]
        and context.awscontainer.vpc
        and context.awscontainer.vpc.efs
    )
    assert context.awscontainer.vpc.efs.local_mount_path == "/mnt/efs"
    assert context.awscontainer.django.caches["default"].model_dump() == {
        "store": "efs",
        "location_subdir": "{stage}",
        "location": "/mnt/efs/dev",
    }
    caches = get_caches(stage="dev", path=toml_path)
    assert caches == {
        "default": {
            "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
            "LOCATION": "/mnt/efs/dev",
        }
    }
