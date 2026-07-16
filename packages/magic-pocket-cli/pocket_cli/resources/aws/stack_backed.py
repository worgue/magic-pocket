from __future__ import annotations

from typing import TYPE_CHECKING

from pocket.resources.base import ResourceStatus

if TYPE_CHECKING:
    from pocket_cli.resources.aws.cloudformation import Stack


class StackBackedResource:
    """CloudFormation stack 1 つに 1:1 で対応するリソースラッパーの共通基底。

    status は stack へ委譲し、create/update は stack 操作 + 完了待ち。
    update は yaml_synced ガード付き (テンプレート同期済みなら no-op)。
    メッセージ出力や mediator 連携が必要なサブクラスは create/update/delete を
    override し、中から _create_stack/_update_stack/_delete_stack を呼ぶ。
    """

    # create/update の wait_status に使う。delete は全リソース共通で 300/10
    wait_timeout: int = 300
    wait_interval: int = 10

    def __init__(self, context) -> None:
        self.context = context

    @property
    def stack(self) -> Stack:
        raise NotImplementedError

    def deploy_init(self):
        pass

    @property
    def status(self) -> ResourceStatus:
        return self.stack.status

    def create(self):
        self._create_stack()

    def update(self):
        self._update_stack()

    def delete(self):
        self._delete_stack()

    def _create_stack(self):
        self.stack.create()
        self.stack.wait_status(
            "COMPLETED", timeout=self.wait_timeout, interval=self.wait_interval
        )

    def _update_stack(self):
        if not self.stack.yaml_synced:
            self.stack.update()
            self.stack.wait_status(
                "COMPLETED", timeout=self.wait_timeout, interval=self.wait_interval
            )

    def _delete_stack(self):
        self.stack.delete()
        self.stack.wait_status("NOEXIST", timeout=300, interval=10)
