from __future__ import annotations

from typing import TYPE_CHECKING

from pocket.resources.base import ResourceStatus
from pocket.utils import echo
from pocket_cli.resources.aws.cloudformation import CloudFrontWafStack

if TYPE_CHECKING:
    from pocket.context import CloudFrontContext


class CloudFrontWaf:
    """us-east-1 に WAFv2 IPSet + WebACL を管理するリソース。

    `[cloudfront.<name>.waf]` block がある CloudFront でのみ使用。
    IPSet の中身は `pocket waf ip ...` CLI で管理する (side-channel)。
    """

    context: CloudFrontContext

    def __init__(self, context: CloudFrontContext) -> None:
        self.context = context

    @property
    def description(self):
        return "Create WAFv2 IPSet + WebACL in us-east-1 for: %s" % self.context.name

    def state_info(self):
        key = "cloudfront-waf-%s" % self.context.name
        return {key: {"name": self.context.name}}

    def deploy_init(self):
        pass

    @property
    def status(self) -> ResourceStatus:
        return self.stack.status

    @property
    def stack(self):
        return CloudFrontWafStack(self.context)

    def create(self):
        echo.log("WAF (IPSet + WebACL) を作成中 (us-east-1)...")
        self.stack.create()
        self.stack.wait_status("COMPLETED", timeout=300, interval=10)
        assert self.context.waf is not None
        if self.context.waf.enable_ip_set:
            echo.info(
                "WAF を作成しました。`pocket waf ip add self --name %s --stage %s` "
                "で自分の IP を allowlist に追加してください "
                "(空の状態では deny-all になります)。"
                % (self.context.name, self.context.stage)
            )
        else:
            echo.info("WAF を作成しました (IP allowlist 無効、managed rules のみ)。")

    def update(self):
        if not self.stack.yaml_synced:
            self.stack.update()
            self.stack.wait_status("COMPLETED", timeout=300, interval=10)

    def delete(self):
        echo.log("WAF スタックを削除中...")
        self.stack.delete()
        self.stack.wait_status("NOEXIST", timeout=300, interval=10)
