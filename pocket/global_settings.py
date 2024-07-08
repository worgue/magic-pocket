import sys
from pathlib import Path

from pydantic_settings import BaseSettings

from .settings import Django
from .utils import get_toml_path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


class GlobalSettings(BaseSettings):
    django_fallback: Django = Django()
    django_test: Django | None = None

    @classmethod
    def from_toml(cls, *, path: str | Path | None = None):
        path = path or get_toml_path()
        data = tomllib.loads(Path(path).read_text())
        return cls.model_validate(data.get("global", {}))
