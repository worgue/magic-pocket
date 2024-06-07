from __future__ import annotations

import json
from functools import cached_property
from typing import TYPE_CHECKING

import boto3

from pocket.utils import echo

if TYPE_CHECKING:
    from pocket.context import SecretsManagerContext


class SecretsManager:
    context: SecretsManagerContext

    def __init__(self, context: SecretsManagerContext) -> None:
        self.context = context
        self.client = boto3.client("secretsmanager", region_name=context.region)

    def update_pocket_secrets(self, secrets: dict[str, str]):
        echo.log("Getting pocket secrets %s ..." % self.context.pocket_key)
        try:
            res = self.client.get_secret_value(SecretId=self.context.pocket_key)
            secret_arn = res["ARN"]
            data = json.loads(res["SecretString"])
        except self.client.exceptions.ResourceNotFoundException:
            data = {}
            secret_arn = None
        if self.context.stage not in data:
            data[self.context.stage] = {}
        data[self.context.stage][self.context.project_name] = secrets
        echo.log("Updating pocket secrets %s ..." % self.context.pocket_key)
        if secret_arn is None:
            self.client.create_secret(
                Name=self.context.pocket_key,
                SecretString=json.dumps(data),
            )
        else:
            self.client.put_secret_value(
                SecretId=secret_arn,
                SecretString=json.dumps(data),
            )
        del self._pocket_secrets_response

    @cached_property
    def _pocket_secrets_response(self):
        echo.log("Requesting pocket secrets %s ..." % self.context.pocket_key)
        try:
            return self.client.get_secret_value(SecretId=self.context.pocket_key)
        except self.client.exceptions.ResourceNotFoundException:
            return None

    @property
    def pocket_secrets_arn(self) -> str:
        if self._pocket_secrets_response:
            return self._pocket_secrets_response["ARN"]
        raise ValueError("Pocket secrets not found")

    @property
    def pocket_secrets(self) -> dict[str, str]:
        if self._pocket_secrets_response is None:
            return {}
        data = json.loads(self._pocket_secrets_response["SecretString"])
        if self.context.stage in data:
            if self.context.project_name in data[self.context.stage]:
                return data[self.context.stage][self.context.project_name]
        return {}

    @cached_property
    def resolved_secrets(self) -> dict[str, str]:
        """These are only containe explicitly defined secrets in the pocket.toml file.
        The variable name is confusing because this was created before pocket_secrets.
        We should rename this to clearer one... someday.
        """

        echo.log("Requesting secrets list...")
        secrets = {}
        for key, arn in self.context.secrets.items():
            res = self.client.get_secret_value(SecretId=arn)
            secrets[key] = res["SecretString"]
        return secrets

    def clear_cache(self):
        if hasattr(self, "resolved_secrets"):
            del self.resolved_secrets
