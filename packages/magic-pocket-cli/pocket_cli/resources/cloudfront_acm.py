from __future__ import annotations

from typing import TYPE_CHECKING

from pocket.resources.base import ResourceStatus
from pocket.utils import echo
from pocket_cli.resources.aws.cloudformation import AcmStack

if TYPE_CHECKING:
    from pocket.context import CloudFrontContext


class CloudFrontAcm:
    """us-east-1 に ACM 証明書を管理するリソース。

    domain が設定されている CloudFront でのみ使用。
    """

    context: CloudFrontContext

    def __init__(self, context: CloudFrontContext) -> None:
        self.context = context

    @property
    def description(self):
        return "Create ACM certificates in us-east-1 for: %s" % self.context.domain

    def state_info(self):
        key = "cloudfront-acm-%s" % self.context.name
        return {key: {"domain": self.context.domain}}

    def deploy_init(self):
        pass

    @property
    def status(self) -> ResourceStatus:
        return self.stack.status

    @property
    def stack(self):
        return AcmStack(self.context)

    def create(self):
        echo.log("ACM 証明書を作成中（DNS 検証の完了まで数分かかります）...")
        self.stack.create()
        self.stack.wait_status("COMPLETED", timeout=600, interval=10)

    def update(self):
        self.stack.update()
        self.stack.wait_status("COMPLETED", timeout=600, interval=10)

    def delete(self):
        self.stack.delete()
        echo.log("ACM スタックを削除中...")
