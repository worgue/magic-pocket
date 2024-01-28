from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING

import boto3

from pocket.utils import console

if TYPE_CHECKING:
    from pocket.context import SecretsManagerContext


class SecretsManager:
    context: SecretsManagerContext

    def __init__(self, context: SecretsManagerContext) -> None:
        self.context = context
        self.client = boto3.client("secretsmanager", region_name=context.region)

    @cached_property
    def secrets(self) -> dict[str, str]:
        console.print("Requesting secrets list...", style="log")
        secrets = {}
        for key, arn in self.context.secrets.items():
            res = self.client.get_secret_value(SecretId=arn)
            secrets[key] = res["SecretString"]
        return secrets

    def clear_cache(self):
        if hasattr(self, "secrets"):
            del self.secrets
