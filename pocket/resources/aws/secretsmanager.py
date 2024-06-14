from __future__ import annotations

import json
from functools import cached_property
from typing import TYPE_CHECKING

import boto3

from pocket.utils import echo

if TYPE_CHECKING:
    from pocket.context import SecretsManagerContext


class PocketSecretIsNotReady(Exception):
    pass


class SecretsManager:
    context: SecretsManagerContext

    def __init__(self, context: SecretsManagerContext) -> None:
        self.context = context
        self.client = boto3.client("secretsmanager", region_name=context.region)

    def delete_pocket_secrets(self):
        echo.log("Deleting pocket secrets %s ..." % self.context.pocket_key)
        res = self._pocket_secrets_response
        if res is None:
            echo.warning("Pocket secrets key was not found")
            return
        data = json.loads(res["SecretString"])
        if data.get(self.context.stage, {}).get(self.context.project_name) is None:
            echo.warning("Pocket secrets entry was not found")
            return
        del data[self.context.stage][self.context.project_name]
        if data[self.context.stage] == {}:
            del data[self.context.stage]
        echo.log("Deleting the entry...")
        self.client.put_secret_value(SecretId=res["ARN"], SecretString=json.dumps(data))
        if data == {}:
            echo.log(f"No entry left, deleting the secret {res['ARN']}...")
            self.client.delete_secret(SecretId=res["ARN"], RecoveryWindowInDays=30)
        del self._pocket_secrets_response

    def update_pocket_secrets(self, secrets: dict[str, str]):
        echo.log("Getting pocket secrets %s ..." % self.context.pocket_key)
        res = self._pocket_secrets_response
        data = json.loads(res["SecretString"]) if res else {}
        if self.context.stage not in data:
            data[self.context.stage] = {}
        data[self.context.stage][self.context.project_name] = secrets
        echo.log("Updating pocket secrets %s ..." % self.context.pocket_key)
        if res is None:
            self.client.create_secret(
                Name=self.context.pocket_key,
                SecretString=json.dumps(data),
            )
        else:
            self.client.put_secret_value(
                SecretId=res["ARN"],
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
        except self.client.exceptions.InvalidRequestException:
            self.client.restore_secret(SecretId=self.context.pocket_key)
            return self.client.get_secret_value(SecretId=self.context.pocket_key)

    @property
    def pocket_secrets_arn(self) -> str:
        if self._pocket_secrets_response:
            return self._pocket_secrets_response["ARN"]
        raise PocketSecretIsNotReady("Pocket secrets not found")

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
    def user_secrets(self) -> dict[str, str]:
        echo.log("Requesting user secrets list...")
        secrets = {}
        for key, arn in self.context.secrets.items():
            res = self.client.get_secret_value(SecretId=arn)
            secrets[key] = res["SecretString"]
        return secrets

    def clear_cache(self):
        if hasattr(self, "resolved_secrets"):
            del self.user_secrets
