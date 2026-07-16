from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from pocket.utils import echo
from pocket_cli.mediator import Mediator
from pocket_cli.resources.aws.cloudformation import CloudFrontKeysStack
from pocket_cli.resources.aws.stack_backed import StackBackedResource

if TYPE_CHECKING:
    from pocket.context import CloudFrontContext


class CloudFrontKeys(StackBackedResource):
    context: CloudFrontContext
    wait_timeout = 120
    wait_interval = 5

    def __init__(self, context: CloudFrontContext) -> None:
        super().__init__(context)
        self._signing_public_key_pem: str = ""

    @property
    def description(self):
        return (
            "Create CloudFront signing key resources for: %s" % self.context.signing_key
        )

    def state_info(self):
        key = "cloudfront-keys-%s" % self.context.name
        return {key: {"signing_key": self.context.signing_key}}

    @property
    def stack(self):
        return CloudFrontKeysStack(
            self.context, signing_public_key_pem=self._signing_public_key_pem
        )

    def prepare_deploy(self, mediator: Mediator | None = None):
        """template hash に影響する公開鍵を store から読み込む (副作用なし)。

        status / yaml_synced の判定前に呼ぶこと。空のまま hash を計算すると
        deploy 済み hash と一致せず、毎回 REQUIRE_UPDATE になる。
        """
        if mediator is None:
            return
        self._prepare_signing_key(mediator)

    # mediator を取るのは意図的な非対称。deploy フロー (_deploy_resource) は
    # inspect.signature で mediator の有無を見て呼び分ける
    def create(self, mediator: Mediator):  # type: ignore[override]
        mediator.ensure_pocket_managed_secrets()
        self._prepare_signing_key(mediator)
        self._create_stack()

    def update(self, mediator: Mediator):  # type: ignore[override]
        mediator.ensure_pocket_managed_secrets()
        self._prepare_signing_key(mediator)
        self._update_stack()

    def delete(self):
        echo.info("Deleting CloudFront keys stack ...")
        self._delete_stack()

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
