"""受信口の安定した命名とworkerへ渡す設定。AWS呼出しは行わない。"""

import hashlib
import re

from pydantic import BaseModel, computed_field

from pocket.settings import Inbound, Settings, parse_handler_ref

# AWS General Reference「Email Receiving endpoints」（2026-09確認）。
RECEIVING_REGIONS = frozenset(
    {
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
        "af-south-1",
        "ap-southeast-3",
        "ap-south-1",
        "ap-northeast-3",
        "ap-northeast-2",
        "ap-southeast-1",
        "ap-southeast-2",
        "ap-northeast-1",
        "ca-central-1",
        "eu-central-1",
        "eu-west-1",
        "eu-west-2",
        "eu-south-1",
        "eu-west-3",
        "eu-north-1",
        "il-central-1",
        "me-south-1",
        "sa-east-1",
    }
)


class InboundContext(BaseModel):
    name: str
    region: str
    resource_name: str
    queue_name: str
    config: Inbound

    @computed_field
    @property
    def bucket_name(self) -> str:
        return self.resource_name + "-${AWS::AccountId}"

    @computed_field
    @property
    def topic_arn(self) -> str:
        return (
            f"arn:${{AWS::Partition}}:sns:{self.region}:"
            f"${{AWS::AccountId}}:{self.resource_name}"
        )

    @computed_field
    @property
    def queue_arn(self) -> str:
        return (
            f"arn:${{AWS::Partition}}:sqs:{self.region}:"
            f"${{AWS::AccountId}}:{self.queue_name}"
        )

    @computed_field
    @property
    def runtime_config(self) -> dict:
        return {
            "region": self.region,
            "bucket": self.bucket_name,
            "topic_arn": self.topic_arn,
            "raw_prefix": self.config.raw_prefix,
            "metadata_prefix": self.config.metadata_prefix,
            "import_prefix": self.config.import_prefix,
            "recipients": self.config.recipients,
        }

    @classmethod
    def from_settings(cls, name: str, root: Settings):
        if root.region not in RECEIVING_REGIONS:
            raise ValueError("SES受信対応リージョンを指定してください")
        config = root.inbound[name]
        container, handler = parse_handler_ref(config.handler)
        base = f"{root.slug}-{root.namespace}-inbound-{name}"
        digest = hashlib.sha256(f"{base}-{root.region}".encode()).hexdigest()[:10]
        resource_name = re.sub("[^a-z0-9-]", "-", base)[:32] + "-" + digest
        prefix = root.prefix_template.format(**root.format_vars)
        return cls(
            name=name,
            region=root.region,
            resource_name=resource_name,
            queue_name=f"{prefix}{container}-{handler}",
            config=config,
        )
