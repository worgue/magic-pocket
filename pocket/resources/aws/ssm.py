from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING

import boto3

from pocket.utils import echo

if TYPE_CHECKING:
    from pocket.context import SecretsContext


class SsmStore:
    context: SecretsContext

    def __init__(self, context: SecretsContext) -> None:
        self.context = context
        self.client = boto3.client("ssm", region_name=context.region)

    def _param_path(self, name: str) -> str:
        return f"/{self.context.pocket_key}/{name}"

    def update_secrets(self, secrets: dict[str, str | dict[str, str]]):
        echo.log("Updating pocket secrets via SSM %s ..." % self.context.pocket_key)
        for key, value in secrets.items():
            if isinstance(value, str):
                self.client.put_parameter(
                    Name=self._param_path(key),
                    Value=value,
                    Type="SecureString",
                    Overwrite=True,
                )
            elif isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    self.client.put_parameter(
                        Name=self._param_path(f"{key}/{sub_key}"),
                        Value=sub_value,
                        Type="SecureString",
                        Overwrite=True,
                    )
        if hasattr(self, "_pocket_secrets_cache"):
            del self._pocket_secrets_cache

    def delete_secrets(self):
        echo.log("Deleting pocket secrets via SSM %s ..." % self.context.pocket_key)
        path = f"/{self.context.pocket_key}/"
        names_to_delete: list[str] = []
        paginator = self.client.get_paginator("get_parameters_by_path")
        for page in paginator.paginate(Path=path, Recursive=True):
            for param in page.get("Parameters", []):
                names_to_delete.append(param["Name"])
        if not names_to_delete:
            echo.warning("No SSM pocket secrets found")
            return
        # DeleteParameters は最大10個ずつ
        for i in range(0, len(names_to_delete), 10):
            batch = names_to_delete[i : i + 10]
            self.client.delete_parameters(Names=batch)
        if hasattr(self, "_pocket_secrets_cache"):
            del self._pocket_secrets_cache

    @cached_property
    def _pocket_secrets_cache(self) -> dict[str, str | dict[str, str]]:
        echo.log("Requesting pocket secrets via SSM %s ..." % self.context.pocket_key)
        path = f"/{self.context.pocket_key}/"
        params: list[dict] = []  # type: ignore[type-arg]
        paginator = self.client.get_paginator("get_parameters_by_path")
        for page in paginator.paginate(Path=path, Recursive=True, WithDecryption=True):
            params.extend(page.get("Parameters", []))
        result: dict[str, str | dict[str, str]] = {}
        for param in params:
            # /{pocket_key}/{env_var_name} or /{pocket_key}/{name}/{sub}
            relative = param["Name"][len(path) :]
            parts = relative.split("/")
            if len(parts) == 1:
                result[parts[0]] = param["Value"]
            elif len(parts) == 2:
                env_key, sub_key = parts
                if env_key not in result:
                    result[env_key] = {}
                entry = result[env_key]
                if isinstance(entry, dict):
                    entry[sub_key] = param["Value"]
        return result

    @property
    def secrets(self) -> dict[str, str | dict[str, str]]:
        return self._pocket_secrets_cache

    @property
    def arn(self) -> str:
        # IAM用ARNパターン
        return (
            "arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/"
            + self.context.pocket_key
            + "/*"
        )
