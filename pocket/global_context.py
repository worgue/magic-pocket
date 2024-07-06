from __future__ import annotations

from pathlib import Path

from pydantic import model_validator

from .context import DjangoContext
from .global_settings import GlobalSettings
from .utils import get_toml_path


class GlobalContext(GlobalSettings):
    django_fallback: DjangoContext | None = None

    @classmethod
    def from_global_settings(cls, global_settings: GlobalSettings) -> GlobalContext:
        data = global_settings.model_dump(by_alias=True)
        return cls.model_validate(data)

    @classmethod
    def from_toml(cls, *, path: str | Path | None = None):
        path = path or get_toml_path()
        return cls.from_global_settings(GlobalSettings.from_toml(path=path))

    @model_validator(mode="after")
    def check_django(self):
        assert self.django_fallback, "django_fallback should be set by settings."
        for _, storage in self.django_fallback.storages.items():
            if storage.store == "s3":
                raise ValueError("s3 storage is not allowed for fallback.")
        return self
