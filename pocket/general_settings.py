import sys
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings

from .django.settings import Django
from .utils import get_project_name, get_toml_path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


class GeneralSettings(BaseSettings):
    object_prefix: str = "pocket-"
    region: str
    project_name: str = Field(default_factory=get_project_name)
    s3_fallback_bucket_name: str | None = None
    django_fallback: Django = Django()
    django_test: Django | None = None

    @classmethod
    def from_toml(cls, *, path: str | Path | None = None):
        path = path or get_toml_path()
        data = tomllib.loads(Path(path).read_text())
        return cls.model_validate(data.get("general", {}))
