from __future__ import annotations

import time
from functools import cached_property
from typing import TYPE_CHECKING, Any

import boto3
import yaml
from botocore.exceptions import ClientError
from deepdiff import DeepDiff
from jinja2 import Environment, PackageLoader, select_autoescape

from pocket.resources.base import ResourceStatus

if TYPE_CHECKING:
    from pocket.context import AwsContainerContext, CloudFrontContext, RdsContext
    from pocket.general_context import VpcContext


class Stack:
    template_filename: str

    @property
    def name(self) -> str:
        raise NotImplementedError

    @property
    def export(self) -> dict:
        raise NotImplementedError

    @property
    def stack_tags(self) -> list[dict]:
        return []

    def __init__(self, context: AwsContainerContext | VpcContext | CloudFrontContext):
        self.context = context
        self.client = self.get_client()

    def get_client(self):
        return boto3.client("cloudformation", region_name=self.context.region)

    def _get_resource(self) -> Any:
        return None

    @property
    def capabilities(self):
        return []

    @cached_property
    def description(self):
        try:
            return self.client.describe_stacks(StackName=self.name)["Stacks"][0]
        except ClientError:
            return None

    @cached_property
    def uploaded_template(self) -> str | None:
        try:
            return self.client.get_template(StackName=self.name)["TemplateBody"]
        except ClientError:
            return None

    def clear_status(self):
        if hasattr(self, "description"):
            del self.description
        if hasattr(self, "uploaded_template"):
            del self.uploaded_template

    def wait_status(
        self,
        status: ResourceStatus,
        timeout=300,
        interval=3,
        error_statuses: tuple[ResourceStatus] = ("FAILED",),
    ):
        for i in range(timeout // interval):
            self.clear_status()
            if self.status == status:
                print("")
                return
            if self.status in error_statuses:
                print(self.description)
                raise Exception(
                    f"Stack status is {self.status}. Please check the console."
                )
            if i == 0:
                msg = "Waiting for %s stack status to be %s" % (
                    self.template_filename,
                    status,
                )
                print(msg, end="", flush=True)
            print(".", end="", flush=True)
            time.sleep(interval)
        raise Exception("Timeout is %s seconds" % timeout)

    @property
    def output(self) -> dict[str, str] | None:
        if self.description and "Outputs" in self.description:
            return {
                output["OutputKey"]: output["OutputValue"]
                for output in self.description["Outputs"]
            }

    @property
    def deleted_at(self):
        if self.description:
            return self.description["DeletionTime"].astimezone()

    @property
    def status_detail(self):
        if not self.description:
            return "NOT_CREATED"
        return self.description["StackStatus"]

    @property
    def exists(self):
        return self.status != "NOEXIST"

    @property
    def status(self) -> ResourceStatus:
        if self.status_detail == "NOT_CREATED":
            return "NOEXIST"
        action = self.status_detail.split("_")[0]
        action_status = self.status_detail.split("_")[-1]
        if action_status == "PROGRESS":
            return "PROGRESS"
        if action_status == "FAILED":
            return "FAILED"
        if action_status == "COMPLETE":
            if action == "DELETE":
                return "NOEXIST"
            elif action == "ROLLBACK":
                if self.deleted_at:
                    return "FAILED"
                return "COMPLETED" if self.yaml_synced else "REQUIRE_UPDATE"
            elif action in {"IMPORT", "REVIEW", "UPDATE", "CREATE"}:
                return "COMPLETED" if self.yaml_synced else "REQUIRE_UPDATE"
        raise Exception("unknown status")

    @property
    def yaml(self) -> str:
        template = Environment(
            loader=PackageLoader("pocket_cli"), autoescape=select_autoescape()
        ).get_template(name=f"cloudformation/{self.template_filename}.yaml")
        original_yaml = template.render(
            stack_name=self.name,
            export=self.export,
            resource=self._get_resource(),
            **self.context.model_dump(),
        )
        return "\n".join(
            [
                line
                for line in original_yaml.splitlines()
                if line.strip() not in ["#", "# prettier-ignore"]
            ]
        )

    @property
    def yaml_synced(self):
        if self.yaml_diff == {}:
            return True
        return False

    @property
    def yaml_diff(self):
        return DeepDiff(
            yaml.safe_load(self.uploaded_template or ""),
            yaml.safe_load(self.yaml),
            ignore_order=True,
        )

    def create(self):
        kwargs: dict[str, Any] = {
            "StackName": self.name,
            "TemplateBody": self.yaml,
            "Capabilities": self.capabilities,
        }
        if self.stack_tags:
            kwargs["Tags"] = self.stack_tags
        return self.client.create_stack(**kwargs)

    def update(self):
        print("Update stack")
        return self.client.update_stack(
            StackName=self.name, TemplateBody=self.yaml, Capabilities=self.capabilities
        )

    def delete(self):
        return self.client.delete_stack(StackName=self.name)


class CloudFrontKeysStack(Stack):
    context: CloudFrontContext
    template_filename = "cloudfront_keys"

    def __init__(
        self,
        context: CloudFrontContext,
        signing_public_key_pem: str = "",
    ):
        self._signing_public_key_pem = signing_public_key_pem
        super().__init__(context)

    def get_client(self):
        return boto3.client("cloudformation", region_name=self.context.region)

    @property
    def name(self):
        return f"{self.context.slug}-cloudfront-keys"

    @property
    def export(self):
        return {
            "public_key_id": f"{self.context.slug}-public-key-id",
            "key_group_id": f"{self.context.slug}-key-group-id",
        }

    @property
    def yaml(self) -> str:
        from jinja2 import Environment, PackageLoader, select_autoescape

        template = Environment(
            loader=PackageLoader("pocket_cli"), autoescape=select_autoescape()
        ).get_template(name=f"cloudformation/{self.template_filename}.yaml")
        original_yaml = template.render(
            stack_name=self.name,
            export=self.export,
            resource=self._get_resource(),
            signing_public_key_pem=self._signing_public_key_pem,
            **self.context.model_dump(),
        )
        return "\n".join(
            [
                line
                for line in original_yaml.splitlines()
                if line.strip() not in ["#", "# prettier-ignore"]
            ]
        )


class CloudFrontStack(Stack):
    context: CloudFrontContext
    template_filename = "cloudfront"

    def __init__(
        self,
        context: CloudFrontContext,
        token_secret_value: str = "",
    ):
        self._token_secret_value = token_secret_value
        super().__init__(context)

    def get_client(self):
        return boto3.client("cloudformation", region_name=self.context.region)

    @property
    def name(self):
        return f"{self.context.slug}-cloudfront"

    @property
    def _has_token_kvs(self) -> bool:
        return any(r.require_token for r in self.context.routes)

    @property
    def export(self):
        exports: dict[str, str] = {}
        if self.context.signing_key:
            exports["key_group_id"] = f"{self.context.slug}-key-group-id"
        if self._has_token_kvs:
            exports["kvs_arn"] = f"{self.context.slug}-token-kvs-arn"
        return exports

    def _resolve_api_origins(self) -> dict[str, str]:
        """Resolve cross-region CloudFormation exports for API origins.

        CloudFormation Fn::ImportValue only works within the same region.
        Since CloudFront stacks run in us-east-1 but Container stacks may be
        in another region, we resolve the exports at render time and embed
        the domain names as literal values.
        """
        if not self.context.api_origins:
            return {}

        cf = boto3.client("cloudformation", region_name=self.context.s3_region)
        exports: dict[str, str] = {}
        paginator = cf.get_paginator("list_exports")
        for page in paginator.paginate():
            for export in page["Exports"]:
                exports[export["Name"]] = export["Value"]

        resolved: dict[str, str] = {}
        for handler_key, export_name in self.context.api_origins.items():
            if export_name not in exports:
                raise RuntimeError(
                    f"CloudFormation export '{export_name}' not found in region "
                    f"'{self.context.s3_region}'. Deploy the Container stack first."
                )
            resolved[handler_key] = exports[export_name]
        return resolved

    def _build_function_codes(self) -> dict[str, str]:
        """ルートごとに CloudFront Function コードを生成する"""
        codes: dict[str, str] = {}
        for route in self.context.routes:
            if not route.is_spa:
                continue
            if route.require_token:
                codes[route.yaml_key] = self._generate_spa_auth_function(route)
            else:
                codes[route.yaml_key] = route.url_fallback_function_indent8
        return codes

    def _generate_spa_auth_function(self, route) -> str:  # type: ignore
        """KVS + HMAC 検証付き async CloudFront Function コードを生成する"""
        fallback_uri = route.path_pattern + "/" + route.spa_fallback_html
        if not route.path_pattern:
            fallback_uri = "/" + route.spa_fallback_html
        login_path = route.login_path
        # ${TokenKvs} は Fn::Sub で CFn が KVS ID に解決する
        code = """\
import cf from 'cloudfront';
const kvsHandle = cf.kvs('${{TokenKvs}}');
async function handler(event) {{
    var request = event.request;
    var lastItem = request.uri.split('/').pop();
    if (!lastItem.includes('.')) {{ request.uri = '{fallback_uri}'; }}
    var cookie = request.cookies['pocket-spa-token'];
    if (!cookie) {{ return _redirect(request); }}
    var parts = cookie.value.split(':');
    if (parts.length !== 3) {{ return _redirect(request); }}
    var expiry = parseInt(parts[1], 10);
    if (Math.floor(Date.now() / 1000) > expiry) {{ return _redirect(request); }}
    var secret;
    try {{ secret = await kvsHandle.get('token_secret'); }}
    catch (e) {{ return _redirect(request); }}
    var msg = parts[0] + ':' + parts[1];
    var key = await crypto.subtle.importKey('raw', _hexToBytes(secret),
        {{name:'HMAC',hash:'SHA-256'}}, false, ['sign']);
    var sig = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(msg));
    if (_bytesToHex(new Uint8Array(sig)) !== parts[2]) {{ return _redirect(request); }}
    return request;
}}
function _redirect(request) {{
    var next = encodeURIComponent(request.uri);
    return {{ statusCode: 302, statusDescription: 'Found',
        headers: {{ location: {{ value: '{login_path}?next=' + next }} }} }};
}}
function _hexToBytes(hex) {{
    var bytes = new Uint8Array(hex.length / 2);
    for (var i = 0; i < hex.length; i += 2) {{
        bytes[i / 2] = parseInt(hex.substr(i, 2), 16);
    }}
    return bytes;
}}
function _bytesToHex(bytes) {{
    var hex = '';
    for (var i = 0; i < bytes.length; i++) {{
        hex += bytes[i].toString(16).padStart(2, '0');
    }}
    return hex;
}}
""".format(fallback_uri=fallback_uri, login_path=login_path)
        # indent 8 spaces for YAML embedding (skip first line)
        lines = []
        for i, line in enumerate(code.splitlines()):
            if i == 0:
                lines.append(line)
            else:
                lines.append(" " * 8 + line)
        return "\n".join(lines)

    @property
    def yaml(self) -> str:
        from jinja2 import Environment, PackageLoader, select_autoescape

        resolved_api_origins = self._resolve_api_origins()
        function_codes = self._build_function_codes()
        template = Environment(
            loader=PackageLoader("pocket_cli"), autoescape=select_autoescape()
        ).get_template(name=f"cloudformation/{self.template_filename}.yaml")
        context_data = self.context.model_dump(
            exclude={"signing_key", "api_origins", "token_secret"}
        )
        original_yaml = template.render(
            stack_name=self.name,
            export=self.export,
            resource=self._get_resource(),
            signing_key=bool(self.context.signing_key),
            api_origins=resolved_api_origins,
            function_codes=function_codes,
            has_token_kvs=self._has_token_kvs,
            **context_data,
        )
        return "\n".join(
            [
                line
                for line in original_yaml.splitlines()
                if line.strip() not in ["#", "# prettier-ignore"]
            ]
        )


class ContainerStack(Stack):
    context: AwsContainerContext
    template_filename = "awscontainer"

    def __init__(
        self,
        context: AwsContainerContext,
        *,
        rds_context: RdsContext | None = None,
    ):
        self._rds_context = rds_context
        super().__init__(context)

    def _resolve_rds(self) -> tuple[str | None, str | None]:
        """RDS の SG ID とシークレット ARN を動的に取得"""
        if self._rds_context is None:
            return None, None
        from pocket_cli.resources.rds import Rds

        rds = Rds(self._rds_context)
        return rds.security_group_id, rds.master_user_secret_arn

    @property
    def name(self):
        return f"{self.context.slug}-container"

    @property
    def capabilities(self):
        return ["CAPABILITY_NAMED_IAM"]

    @property
    def export(self):
        if self.context.vpc:
            return VpcStack(self.context.vpc).export
        return {}

    def _resolve_vpc_zone_count(self) -> int:
        assert self.context.vpc, "VPC context is required"
        vpc_stack = VpcStack(self.context.vpc)
        output = vpc_stack.output
        assert output, f"VPC stack '{vpc_stack.name}' の output が取得できません"
        prefix = vpc_stack.export["private_subnet_"]
        count = 0
        for i in range(1, 20):
            if f"{prefix}{i}" in output:
                count += 1
            else:
                break
        return count

    @property
    def yaml(self) -> str:
        rds_sg_id, rds_secret_arn = self._resolve_rds()
        context_dump = self.context.model_dump()

        # 外部 VPC: zones を動的取得
        if self.context.vpc and not self.context.vpc.manage:
            zone_count = self._resolve_vpc_zone_count()
            context_dump["vpc"]["zones"] = [
                f"{self.context.vpc.region}{chr(97 + i)}" for i in range(zone_count)
            ]

        template = Environment(
            loader=PackageLoader("pocket_cli"), autoescape=select_autoescape()
        ).get_template(name=f"cloudformation/{self.template_filename}.yaml")
        original_yaml = template.render(
            stack_name=self.name,
            export=self.export,
            resource=self._get_resource(),
            rds_security_group_id=rds_sg_id,
            rds_secret_arn=rds_secret_arn,
            use_rds=rds_sg_id is not None,
            **context_dump,
        )
        return "\n".join(
            [
                line
                for line in original_yaml.splitlines()
                if line.strip() not in ["#", "# prettier-ignore"]
            ]
        )


class VpcStack(Stack):
    context: VpcContext
    template_filename = "vpc"

    def _get_resource(self):
        from pocket_cli.resources.vpc import Vpc

        return Vpc(self.context)

    @property
    def name(self):
        return f"{self.context.name}-vpc"

    @property
    def export(self):
        return {
            "vpc_id": self.context.name + "-vpc-id",
            "private_subnet_": self.context.name + "-private-subnet-",
            "efs_access_point_arn": self.context.name + "-efs-access-point",
            "efs_security_group": self.context.name + "-efs-security-group",
        }

    @property
    def stack_tags(self) -> list[dict]:
        tags: list[dict[str, str]] = []
        if self.context.sharable:
            tags.append({"Key": "pocket:sharable", "Value": "true"})
        return tags

    @cached_property
    def tags(self) -> list[dict]:
        if self.description:
            return self.description.get("Tags", [])
        return []

    @cached_property
    def stack_arn(self) -> str | None:
        if self.description:
            return self.description["StackId"]  # type: ignore
        return None

    def get_tag(self, key: str) -> str | None:
        for tag in self.tags:
            if tag["Key"] == key:
                return tag["Value"]  # type: ignore
        return None

    @property
    def is_sharable(self) -> bool:
        return self.get_tag("pocket:sharable") == "true"

    @property
    def consumers(self) -> list[str]:
        return [
            t["Key"].removeprefix("pocket:consumer:")
            for t in self.tags
            if t["Key"].startswith("pocket:consumer:")
        ]

    def add_consumer_tag(self, slug: str):
        if not self.stack_arn:
            return
        tagging = boto3.client(
            "resourcegroupstaggingapi", region_name=self.context.region
        )
        tagging.tag_resources(
            ResourceARNList=[self.stack_arn],
            Tags={f"pocket:consumer:{slug}": "deployed"},
        )

    def remove_consumer_tag(self, slug: str):
        if not self.stack_arn:
            return
        tagging = boto3.client(
            "resourcegroupstaggingapi", region_name=self.context.region
        )
        tagging.untag_resources(
            ResourceARNList=[self.stack_arn],
            TagKeys=[f"pocket:consumer:{slug}"],
        )
