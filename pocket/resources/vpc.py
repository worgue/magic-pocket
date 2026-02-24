from __future__ import annotations

from typing import TYPE_CHECKING

from .aws.cloudformation import VpcStack
from .aws.efs import Efs
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
    def efs(self):
        if self.context.efs:
            return Efs(self.context.efs)

    @property
    def status(self) -> ResourceStatus:
        return self.stack.status

    @property
    def vpc_id(self):
        if self.stack.output:
            return self.stack.output[self.stack.export["vpc_id"]]

    @property
    def description(self):
        description = "Create aws cloudformation stack: %s" % self.stack.name
        if self.efs:
            description += "\nCreate efs: %s" % self.efs.context.name
        return description

    def state_info(self):
        if self.context.efs:
            return {"efs": {"name": self.context.efs.name}}
        return {}

    def deploy_init(self):
        if self.efs:
            self.efs.ensure_exists()

    def create(self):
        print("Creating cloudformation stack for vpc ...")
        self.stack.create()

    def delete(self):
        if self.stack.status != "NOEXIST":
            self.stack.delete()
            self.stack.wait_status("NOEXIST", timeout=300, interval=10)
        if self.efs and self.efs.exists():
            self.efs.delete()

    def update(self):
        if not self.stack.yaml_synced:
            self.stack.update()
