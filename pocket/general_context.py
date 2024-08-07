from __future__ import annotations

from pathlib import Path

from pydantic import model_validator

from .context import DjangoContext
from .general_settings import GeneralSettings
from .utils import get_toml_path


class GeneralContext(GeneralSettings):
    django_fallback: DjangoContext | None = None
    django_test: DjangoContext | None = None

    @classmethod
    def from_general_settings(cls, general_settings: GeneralSettings) -> GeneralContext:
        data = general_settings.model_dump(by_alias=True)
        return cls.model_validate(data)

    @classmethod
    def from_toml(cls, *, path: str | Path | None = None):
        path = path or get_toml_path()
        return cls.from_general_settings(GeneralSettings.from_toml(path=path))

    @model_validator(mode="after")
    def check_django(self):
        assert self.django_fallback, "django_fallback should be set by settings."
        for _, storage in self.django_fallback.storages.items():
            if storage.store == "s3" and not self.s3_fallback_bucket_name:
                raise ValueError(
                    "s3_fallback_bucket_name is required "
                    "to use s3 storage is fallback_context."
                )
        return self
