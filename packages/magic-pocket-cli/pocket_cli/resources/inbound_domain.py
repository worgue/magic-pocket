"""受信ドメインの所有 (SES identity / DKIM / MX) を持つstackのライフサイクル。"""

import json
import time
from functools import cached_property

import boto3
from botocore.exceptions import ClientError

from pocket.context import resolve_hosted_zone_id
from pocket.inbound_context import InboundDomainContext
from pocket.utils import echo
from pocket_cli.resources.aws.cloudformation import Stack, _is_stack_not_exist_error
from pocket_cli.resources.aws.stack_backed import StackBackedResource
from pocket_cli.resources.inbound_template import DKIM_TOKENS, build_domain_template

VERIFY_TIMEOUT = 600
VERIFY_INTERVAL = 15
STATUS_COMMAND = "pocket resource inbound --stage <stage> --name <name> status"


class InboundDomainStack(Stack):
    context: InboundDomainContext
    template_filename = "inbound_domain"

    @property
    def name(self):
        return self.context.stack_name

    @property
    def export(self):
        return {}

    @cached_property
    def hosted_zone_id(self) -> str | None:
        if not self.context.manage_dns:
            return None
        # zone が見つからなければここで止まる。外部 DNS は manage_dns = false を
        # 明示させ、黙って手動登録へ fallback しない
        return resolve_hosted_zone_id(
            self.context.domain, self.context.hosted_zone_id_override
        )

    @property
    def yaml(self):
        return json.dumps(
            build_domain_template(self.context, self.hosted_zone_id), indent=2
        )


class InboundDomain(StackBackedResource):
    context: InboundDomainContext

    @cached_property
    def _stack(self) -> InboundDomainStack:
        return InboundDomainStack(self.context)

    @property
    def stack(self) -> InboundDomainStack:
        return self._stack

    def verification_status(self) -> str | None:
        """SESv2のVerificationStatus。identityが無ければNone。"""
        sesv2 = boto3.client("sesv2", region_name=self.context.region)
        try:
            identity = sesv2.get_email_identity(EmailIdentity=self.context.domain)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "NotFoundException":
                return None
            raise
        return identity.get("VerificationStatus")

    def prepare_deploy(self, mediator=None):
        if self.stack.cfn_status != "NOEXIST":
            return
        if self.verification_status() is None:
            return
        domain = self.context.domain
        raise ValueError(
            f"{domain} のSES identityが既に存在します。"
            "pocketは既存のidentityを取り込みません。"
            "受信を別のサブドメインにするか、次で削除してからdeployしてください"
            "（手で登録した検証用・MXのDNSレコードも削除が必要です）:\n"
            f"  aws sesv2 delete-email-identity --email-identity {domain}"
            f" --region {self.context.region}"
        )

    def state_info(self):
        return {
            "inbound_domain": {self.context.domain: {"stack_name": self.stack.name}}
        }

    def delete(self):
        if self.stack.cfn_status == "NOEXIST":
            return
        self._delete_stack()

    def dns_records(self) -> list[tuple[str, str, str]]:
        """登録が必要な (type, name, value)。stack未作成なら空。"""
        output = self.stack.output
        if not output:
            return []
        records = [
            ("CNAME", output[f"DkimName{i}"], output[f"DkimValue{i}"])
            for i in DKIM_TOKENS
        ]
        records.append(("MX", self.context.domain, output["MxValue"]))
        return records

    def wait_verified(self) -> int:
        """DKIM検証の完了を待ち、待った秒数を返す。同じaccountのzoneなら数分で終わる。"""
        started = time.monotonic()
        deadline = started + VERIFY_TIMEOUT
        while True:
            status = self.verification_status()
            if status == "SUCCESS":
                return round(time.monotonic() - started)
            if time.monotonic() >= deadline:
                raise ValueError(
                    f"{self.context.domain} のSES検証が完了しません"
                    f"（status={status}）。zoneの委任を確認し、"
                    "検証後に再度deployしてください。検証完了までは受信できません。"
                    f"状態は {STATUS_COMMAND} で確認できます"
                )
            echo.log(
                "Waiting for SES verification of %s... (status=%s)"
                % (self.context.domain, status)
            )
            time.sleep(VERIFY_INTERVAL)


def confirm_inbound_domains(context):
    """deployの最後に受信できる状態かを確定する。

    pocketがDNSを持つdomainは検証完了まで待つ。外部DNSは待たずに、
    登録すべきレコードと現在の検証状態を示す。
    """
    for domain_ctx in context.inbound_domain.values():
        resource = InboundDomain(domain_ctx)
        if domain_ctx.manage_dns:
            waited = resource.wait_verified()
            echo.info(
                f"inbound domain {domain_ctx.domain}: verification=SUCCESS"
                f" (waited {waited}s)"
            )
            continue
        status = resource.verification_status()
        if status == "SUCCESS":
            echo.info(
                f"inbound domain {domain_ctx.domain}: verification=SUCCESS"
                " (manage_dns = false)"
            )
            continue
        echo.warning(
            f"inbound domain {domain_ctx.domain}: verification={status}"
            " (manage_dns = false のため待たない)。"
            "次のレコードをDNSに登録してください。検証完了までは受信できません。"
            f"状態は {STATUS_COMMAND} で確認できます"
        )
        print_dns_records(resource)


def cleanup_unused_inbound_domains(context, state_store):
    """どのinboundも使わなくなったdomainのstackを消す (inboundのdomain変更後)。"""
    saved = state_store.load().get("resources", {}).get("inbound_domain", {})
    client = boto3.client("cloudformation", region_name=context.general.region)
    for domain, info in saved.items():
        if domain in context.inbound_domain:
            continue
        try:
            client.describe_stacks(StackName=info["stack_name"])
        except ClientError as error:
            if _is_stack_not_exist_error(error):
                continue
            raise
        echo.log("Deleting unused inbound domain %s..." % domain)
        client.delete_stack(StackName=info["stack_name"])


def print_dns_records(resource: InboundDomain):
    for record_type, name, value in resource.dns_records():
        print(f"  {record_type} {name} {value}")
