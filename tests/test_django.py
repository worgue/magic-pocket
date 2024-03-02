from pocket.context import Context, DjangoStorageContext
from pocket.django.utils import get_caches, get_storages


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
            "OPTIONS": {"bucket_name": "pocket-dev-testprj", "location": "media"},
        },
        "staticfiles": {
            "BACKEND": "storages.backends.s3boto3.S3ManifestStaticStorage",
            "OPTIONS": {"bucket_name": "pocket-dev-testprj", "location": "static"},
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
        "subdir": "{stage}",
        "location": "/mnt/efs/dev",
    }
    caches = get_caches(stage="dev", path=toml_path)
    assert caches == {
        "default": {
            "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
            "LOCATION": "/mnt/efs/dev",
        }
    }
