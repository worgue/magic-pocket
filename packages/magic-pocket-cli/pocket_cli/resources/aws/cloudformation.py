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
    from pocket.context import (
        AwsContainerContext,
        CloudFrontContext,
        DsqlContext,
        RdsContext,
    )
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
        noexist_count = 0
        for i in range(timeout // interval):
            self.clear_status()
            current = self.cfn_status
            if current == status:
                print("")
                return
            if current in error_statuses:
                print(self.description)
                raise RuntimeError(
                    f"Stack status is {current}. Please check the console."
                )
            # COMPLETED を待っているのにスタックが見つからない場合
            if status != "NOEXIST" and current == "NOEXIST":
                noexist_count += 1
                if noexist_count >= 3:
                    raise RuntimeError(
                        f"Stack '{self.name}' が見つかりません。"
                        "作成に失敗した可能性があります。"
                        "AWS コンソールで権限とリージョンを確認してください。"
                    )
            else:
                noexist_count = 0
            if i == 0:
                msg = "Waiting for %s stack status to be %s" % (
                    self.template_filename,
                    status,
                )
                print(msg, end="", flush=True)
            print(".", end="", flush=True)
            time.sleep(interval)
        raise RuntimeError("Timeout is %s seconds" % timeout)

    @property
    def output(self) -> dict[str, str] | None:
        if self.description and "Outputs" in self.description:
            result = {}
            for output in self.description["Outputs"]:
                result[output["OutputKey"]] = output["OutputValue"]
                if "ExportName" in output:
                    result[output["ExportName"]] = output["OutputValue"]
            return result

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
    def cfn_status(self) -> ResourceStatus:
        """CloudFormation のステータスのみで判定する（yaml_synced を含まない）。

        wait_status 等、スタック操作の完了待ちに使用する。
        """
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
                return "COMPLETED"
            elif action in {"IMPORT", "REVIEW", "UPDATE", "CREATE"}:
                return "COMPLETED"
        raise RuntimeError("unknown status: %s" % self.status_detail)

    @property
    def status(self) -> ResourceStatus:
        """deploy_resources 等で使うステータス。yaml_synced も考慮する。"""
        cfn = self.cfn_status
        if cfn in ("NOEXIST", "PROGRESS", "FAILED"):
            return cfn
        # COMPLETED だが yaml が同期していなければ更新が必要
        if cfn == "COMPLETED" and not self.yaml_synced:
            return "REQUIRE_UPDATE"
        return cfn

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
    def _template_hash(self) -> str:
        """現在のテンプレートの SHA256 ハッシュ"""
        import hashlib

        return hashlib.sha256(self.yaml.encode()).hexdigest()[:16]

    @property
    def _deployed_template_hash(self) -> str | None:
        """デプロイ済みスタックのタグから template hash を取得"""
        if not self.description:
            return None
        for tag in self.description.get("Tags", []):
            if tag["Key"] == "pocket:template_hash":
                return tag["Value"]
        return None

    @property
    def yaml_synced(self):
        deployed_hash = self._deployed_template_hash
        if deployed_hash is None:
            if self.cfn_status not in ("NOEXIST",):
                print(
                    "pocket:template_hash タグがありません: %s\n"
                    "pocket migrate --stage=<stage> を実行してください。" % self.name
                )
            return False
        return deployed_hash == self._template_hash

    @property
    def yaml_diff(self):
        return DeepDiff(
            yaml.safe_load(self.uploaded_template or ""),
            yaml.safe_load(self.yaml),
            ignore_order=True,
        )

    def _build_tags(self) -> list[dict]:
        tags = list(self.stack_tags)
        tags.append({"Key": "pocket:template_hash", "Value": self._template_hash})
        return tags

    def create(self):
        kwargs: dict[str, Any] = {
            "StackName": self.name,
            "TemplateBody": self.yaml,
            "Capabilities": self.capabilities,
            "Tags": self._build_tags(),
        }
        return self.client.create_stack(**kwargs)

    def update(self):
        print("Update stack")
        return self.client.update_stack(
            StackName=self.name,
            TemplateBody=self.yaml,
            Capabilities=self.capabilities,
            Tags=self._build_tags(),
        )

    def delete(self):
        return self.client.delete_stack(StackName=self.name)


class AcmStack(Stack):
    """us-east-1 に ACM 証明書を作成するスタック。

    CloudFront はカスタムドメイン使用時に us-east-1 の ACM 証明書が必須。
    メインの CloudFront スタックとは別リージョンで管理する。
    """

    context: CloudFrontContext
    template_filename = "cloudfront_acm"

    def get_client(self):
        return boto3.client("cloudformation", region_name="us-east-1")

    @property
    def name(self):
        return f"{self.context.slug}-acm"

    @property
    def export(self):
        return {}


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

    def _resolve_acm_arns(self) -> tuple[str | None, dict[str, str]]:
        """ACM スタック (us-east-1) から証明書 ARN を取得する。

        Returns:
            (メインドメインの証明書 ARN, redirect_from の yaml_key → ARN マップ)
        """
        if not self.context.domain:
            return None, {}
        acm_stack = AcmStack(self.context)
        output = acm_stack.output
        if not output:
            raise RuntimeError(
                f"ACM stack '{acm_stack.name}' が見つかりません。"
                "先に ACM スタックをデプロイしてください。"
            )
        cert_arn = output.get("CertificateArn")
        redirect_arns: dict[str, str] = {}
        for rf in self.context.redirect_from:
            key = f"CertificateArn{rf.yaml_key}"
            if key in output:
                redirect_arns[rf.yaml_key] = output[key]
        return cert_arn, redirect_arns

    def _resolve_api_origins(self) -> dict[str, str]:
        """Container スタックの API ドメインエクスポートをレンダリング時に解決する。

        CloudFront テンプレートに literal 値として埋め込む。
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
                codes[route.yaml_key] = self._generate_spa_fallback_function(route)
        return codes

    def _generate_spa_fallback_function(self, route) -> str:  # type: ignore
        """SPA URL フォールバック用 CloudFront Function コードを生成する"""
        fallback_uri = route.path_pattern + "/" + route.spa_fallback_html
        if not route.path_pattern:
            fallback_uri = "/" + route.spa_fallback_html
        env = Environment(
            loader=PackageLoader("pocket_cli"),
            autoescape=select_autoescape(),
        )
        template = env.get_template("cloudformation/cf_function_spa_fallback.js")
        code = template.render(fallback_uri=fallback_uri)
        # FunctionCode: | の下は8スペース
        lines = []
        for i, line in enumerate(code.splitlines()):
            if i == 0:
                lines.append(line)
            else:
                lines.append(" " * 8 + line)
        return "\n".join(lines)

    def _generate_spa_auth_function(self, route) -> str:  # type: ignore
        """KVS + HMAC 検証付き async CloudFront Function コードを生成する"""
        fallback_uri = route.path_pattern + "/" + route.spa_fallback_html
        if not route.path_pattern:
            fallback_uri = "/" + route.spa_fallback_html
        env = Environment(
            loader=PackageLoader("pocket_cli"),
            autoescape=select_autoescape(),
        )
        template = env.get_template("cloudformation/cf_function_spa_auth.js")
        code = template.render(
            fallback_uri=fallback_uri,
            login_path=route.login_path,
        )
        # Fn::Sub の2パラメータ形式で - | の下は12スペース
        lines = []
        for i, line in enumerate(code.splitlines()):
            if i == 0:
                lines.append(line)
            else:
                lines.append(" " * 12 + line)
        return "\n".join(lines)

    def _generate_api_host_function(self) -> str:
        """API ルート用 X-Forwarded-Host 付与 Function コードを生成する"""
        from jinja2 import Environment, PackageLoader, select_autoescape

        env = Environment(
            loader=PackageLoader("pocket_cli"),
            autoescape=select_autoescape(),
        )
        template = env.get_template("cloudformation/cf_function_api_host.js")
        code = template.render()
        lines = []
        for i, line in enumerate(code.splitlines()):
            if i == 0:
                lines.append(line)
            else:
                lines.append(" " * 8 + line)
        return "\n".join(lines)

    @property
    def yaml(self) -> str:
        resolved_api_origins = self._resolve_api_origins()
        acm_certificate_arn, acm_redirect_arns = self._resolve_acm_arns()
        function_codes = self._build_function_codes()
        api_host_function_code = ""
        if self.context.api_routes:
            api_host_function_code = self._generate_api_host_function()

        from jinja2 import Environment, PackageLoader, select_autoescape

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
            acm_certificate_arn=acm_certificate_arn,
            acm_redirect_arns=acm_redirect_arns,
            function_codes=function_codes,
            api_host_function_code=api_host_function_code,
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
        dsql_context: DsqlContext | None = None,
    ):
        self._rds_context = rds_context
        self._dsql_context = dsql_context
        super().__init__(context)

    def _resolve_rds(self) -> dict:
        """RDS の接続情報を動的に取得"""
        if self._rds_context is None:
            return {}
        from pocket_cli.resources.rds import Rds

        rds = Rds(self._rds_context)
        return {
            "rds_security_group_id": rds.security_group_id,
            "rds_secret_arn": rds.master_user_secret_arn,
            "rds_kms_key_id": rds.master_user_secret_kms_key_id,
            "rds_endpoint": rds.endpoint,
            "rds_port": str(rds.port) if rds.port else None,
            "rds_dbname": rds.database_name,
        }

    def _resolve_dsql(self) -> tuple[str | None, str | None, str | None]:
        """DSQL のエンドポイント、リージョン、ARN を動的に取得"""
        if self._dsql_context is None:
            return None, None, None
        from pocket_cli.resources.dsql import Dsql

        dsql = Dsql(self._dsql_context)
        return dsql.endpoint, self._dsql_context.region, dsql.arn

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
        rds_info = self._resolve_rds()
        dsql_endpoint, dsql_region, dsql_cluster_arn = self._resolve_dsql()
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
            rds_security_group_id=rds_info.get("rds_security_group_id"),
            rds_secret_arn=rds_info.get("rds_secret_arn"),
            rds_kms_key_id=rds_info.get("rds_kms_key_id"),
            rds_endpoint=rds_info.get("rds_endpoint"),
            rds_port=rds_info.get("rds_port"),
            rds_dbname=rds_info.get("rds_dbname"),
            use_rds=bool(rds_info),
            dsql_endpoint=dsql_endpoint,
            dsql_region=dsql_region,
            dsql_cluster_arn=dsql_cluster_arn,
            use_dsql=dsql_endpoint is not None,
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
