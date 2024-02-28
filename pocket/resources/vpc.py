from __future__ import annotations

from typing import TYPE_CHECKING

from .aws.cloudformation import VpcStack
from .base import ResourceStatus

if TYPE_CHECKING:
    from ..context import VpcContext


class Vpc:
    context: VpcContext

    def __init__(self, context: VpcContext):
        self.context = context
        self.main_context = context

    @property
    def stack(self):
        return VpcStack(self.context)

    @property
    def status(self) -> ResourceStatus:
        return self.stack.status

    @property
    def vpc_id(self):
        if self.stack.output:
            return self.stack.output[self.stack.export["vpc_id"]]

    def deploy_init(self):
        pass

    def create(self):
        print("Creating cloudformation stack for vpc ...")
        self.stack.create()

    def update(self):
        if not self.stack.yaml_synced:
            self.stack.update()
