from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from pocket.resources.base import ResourceStatus
from pocket.utils import echo
from pocket_cli.mediator import Mediator
from pocket_cli.resources.aws.cloudformation import CloudFrontKeysStack

if TYPE_CHECKING:
    from pocket.context import CloudFrontContext


class CloudFrontKeys:
    context: CloudFrontContext

    def __init__(self, context: CloudFrontContext) -> None:
        self.context = context
        self._signing_public_key_pem: str = ""

    @property
    def description(self):
        return (
            "Create CloudFront signing key resources for: %s" % self.context.signing_key
        )

    def state_info(self):
        key = "cloudfront-keys-%s" % self.context.name
        return {key: {"signing_key": self.context.signing_key}}

    def deploy_init(self):
        pass

    @property
    def status(self) -> ResourceStatus:
        return self.stack.status

    @property
    def stack(self):
        return CloudFrontKeysStack(
            self.context, signing_public_key_pem=self._signing_public_key_pem
        )

    def create(self, mediator: Mediator):
        mediator.ensure_pocket_managed_secrets()
        self._prepare_signing_key(mediator)
        self.stack.create()
        self.stack.wait_status("COMPLETED", timeout=120, interval=5)

    def update(self, mediator: Mediator):
        mediator.ensure_pocket_managed_secrets()
        self._prepare_signing_key(mediator)
        if not self.stack.yaml_synced:
            self.stack.update()
            self.stack.wait_status("COMPLETED", timeout=120, interval=5)

    def delete(self):
        self.stack.delete()
        echo.info("Deleting CloudFront keys stack ...")

    def _prepare_signing_key(self, mediator: Mediator):
        if not self.context.signing_key:
            return
        ac = mediator.context.awscontainer
        if ac is None or ac.secrets is None:
            echo.warning("awscontainer.secrets is not configured.")
            return
        secrets = ac.secrets.pocket_store.secrets
        signing_key_name = self.context.signing_key
        if signing_key_name not in secrets:
            echo.warning(
                "signing_key '%s' not found in managed secrets. "
                "Deploy the container first to generate the key." % signing_key_name
            )
            return
        secret_data = secrets[signing_key_name]
        if isinstance(secret_data, dict) and "pub" in secret_data:
            pub_b64 = secret_data["pub"]
            self._signing_public_key_pem = base64.b64decode(pub_b64).decode("utf-8")
