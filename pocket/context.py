from __future__ import annotations

import re
from functools import cached_property
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, computed_field, model_validator

from . import settings
from .django.context import DjangoContext
from .general_context import GeneralContext, VpcContext
from .resources.aws.secretsmanager import PocketSecretIsNotReady, SecretsManager
from .resources.aws.ssm import SsmStore
from .settings import ManagedSecretSpec, StoreType, UserSecretSpec
from .utils import echo, get_hosted_zone_id_from_domain, get_toml_path


class ApiGatewayContext(BaseModel):
    domain: str | None = None
    create_records: bool = True
    hosted_zone_id_override: str | None = None

    @computed_field
    def disable_execute_api_endpoint(self) -> bool:
        return bool(self.domain)

    @computed_field
    @cached_property
    def hosted_zone_id(self) -> str | None:
        if self.hosted_zone_id_override:
            return self.hosted_zone_id_override
        if self.domain and self.create_records:
            return get_hosted_zone_id_from_domain(self.domain)
        return None

    @classmethod
    def from_settings(cls, apigw: settings.ApiGateway) -> ApiGatewayContext:
        return cls(
            domain=apigw.domain,
            create_records=apigw.create_records,
            hosted_zone_id_override=apigw.hosted_zone_id_override,
        )


class SqsContext(BaseModel):
    batch_size: int = 10
    message_retention_period: int = 345600
    maximum_concurrency: int = 2
    dead_letter_max_receive_count: int = 5
    dead_letter_message_retention_period: int = 1209600
    report_batch_item_failures: bool = True
    name: str
    visibility_timeout: int

    @classmethod
    def from_settings(
        cls,
        sqs: settings.Sqs,
        *,
        resource_prefix: str,
        key: str,
        timeout: int,
    ) -> SqsContext:
        return cls(
            batch_size=sqs.batch_size,
            message_retention_period=sqs.message_retention_period,
            maximum_concurrency=sqs.maximum_concurrency,
            dead_letter_max_receive_count=sqs.dead_letter_max_receive_count,
            dead_letter_message_retention_period=sqs.dead_letter_message_retention_period,
            report_batch_item_failures=sqs.report_batch_item_failures,
            name=f"{resource_prefix}{key}",
            visibility_timeout=timeout * 6,
        )


class LambdaHandlerContext(BaseModel):
    command: str
    timeout: int = 30
    memory_size: int = 512
    reserved_concurrency: int | None = None
    region: str
    apigateway: ApiGatewayContext | None = None
    sqs: SqsContext | None = None
    key: str
    function_name: str
    log_group_name: str

    @computed_field
    @property
    def cloudformation_cert_ref_name(self) -> str:
        return self.key.capitalize() + "Certificate"

    @classmethod
    def from_settings(
        cls,
        handler: settings.LambdaHandler,
        *,
        key: str,
        root: settings.Settings,
        resource_prefix: str,
    ) -> LambdaHandlerContext:
        apigw_ctx = None
        if handler.apigateway:
            apigw_ctx = ApiGatewayContext.from_settings(handler.apigateway)
        sqs_ctx = None
        if handler.sqs:
            sqs_ctx = SqsContext.from_settings(
                handler.sqs,
                resource_prefix=resource_prefix,
                key=key,
                timeout=handler.timeout,
            )
        function_name = f"{resource_prefix}{key}"
        return cls(
            command=handler.command,
            timeout=handler.timeout,
            memory_size=handler.memory_size,
            reserved_concurrency=handler.reserved_concurrency,
            region=root.region,
            apigateway=apigw_ctx,
            sqs=sqs_ctx,
            key=key,
            function_name=function_name,
            log_group_name=f"/aws/lambda/{function_name}",
        )


class SecretsContext(BaseModel):
    store: StoreType = "sm"
    managed: dict[str, ManagedSecretSpec] = {}
    user: dict[str, UserSecretSpec] = {}
    extra_resources: list[str] = []
    require_list_secrets: bool = False
    region: str
    pocket_key: str
    stage: str
    project_name: str

    @cached_property
    def pocket_store(self):
        """storeに応じたpocket secrets操作クラスを返す"""
        if self.store == "ssm":
            return SsmStore(self)
        return SecretsManager(self)

    def _ensure_sm_arn(self, resource: str):
        if resource.startswith("arn:"):
            return resource
        return (
            "arn:aws:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:" + resource
        )

    def _ensure_ssm_arn(self, resource: str):
        if resource.startswith("arn:"):
            return resource
        return "arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter" + resource

    @computed_field
    @cached_property
    def allowed_sm_resources(self) -> list[str]:
        resources: list[str] = []
        for spec in self.user.values():
            effective_store = spec.store or self.store
            if effective_store == "sm":
                resources.append(spec.name)
        if self.managed and self.store == "sm":
            try:
                resources.append(self.pocket_store.arn)
            except PocketSecretIsNotReady:
                echo.warning(
                    "Pocket managed secrets is not ready. "
                    "The context is not complete data.\n"
                    "Use deploy command or create secrets before create awslambda."
                )
        sm_extras = [
            r
            for r in self.extra_resources
            if ":secretsmanager:" in r or not r.startswith("arn:")
        ]
        resources += sm_extras
        return [self._ensure_sm_arn(r) for r in resources if r]

    @computed_field
    @cached_property
    def allowed_ssm_resources(self) -> list[str]:
        resources: list[str] = []
        for spec in self.user.values():
            effective_store = spec.store or self.store
            if effective_store == "ssm":
                resources.append(spec.name)
        if self.managed and self.store == "ssm":
            resources.append(
                "arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/"
                + self.pocket_key
                + "/*"
            )
        ssm_extras = [r for r in self.extra_resources if ":ssm:" in r]
        resources += ssm_extras
        return [self._ensure_ssm_arn(r) for r in resources if r]

    @model_validator(mode="after")
    def check_entry(self):
        has_resources = (
            self.allowed_sm_resources
            or self.allowed_ssm_resources
            or self.require_list_secrets
        )
        if not has_resources:
            echo.log(
                "No secret resouces are associated to lambda."
                "The data is for reference only."
            )
        return self

    @classmethod
    def from_settings(
        cls, secrets: settings.Secrets, root: settings.Settings
    ) -> SecretsContext:
        format_vars = {
            "namespace": root.namespace,
            "stage": root.stage,
            "project": root.project_name,
        }
        return cls(
            store=secrets.store,
            managed=secrets.managed,
            user=secrets.user,
            extra_resources=secrets.extra_resources,
            require_list_secrets=secrets.require_list_secrets,
            region=root.region,
            pocket_key=secrets.pocket_key_format.format(**format_vars),
            stage=root.stage,
            project_name=root.project_name,
        )


class AwsContainerContext(BaseModel):
    vpc: VpcContext | None = None
    secrets: SecretsContext | None = None
    dockerfile_path: str
    envs: dict[str, str] = {}
    platform: str = "linux/amd64"
    django: DjangoContext | None = None
    region: str
    slug: str
    stage: str
    namespace: str
    resource_prefix: str
    handlers: dict[str, LambdaHandlerContext] = {}
    ecr_name: str
    use_s3: bool
    use_route53: bool = False
    use_sqs: bool = False
    use_efs: bool = False
    permissions_boundary: str | None = None
    efs_local_mount_path: str = ""

    @classmethod
    def from_settings(
        cls, ac: settings.AwsContainer, root: settings.Settings
    ) -> AwsContainerContext:
        resource_prefix = root.prefix_template.format(
            stage=root.stage,
            project=root.project_name,
            namespace=root.namespace,
        )

        vpc_ctx = None
        if ac.vpc:
            vpc_ctx = VpcContext.from_settings(ac.vpc, root.general)

        secrets_ctx = None
        if ac.secrets:
            secrets_ctx = SecretsContext.from_settings(ac.secrets, root)

        handlers = {}
        use_route53 = False
        use_sqs = False
        for key, handler in ac.handlers.items():
            handlers[key] = LambdaHandlerContext.from_settings(
                handler, key=key, root=root, resource_prefix=resource_prefix
            )
            if handler.apigateway:
                use_route53 = True
            if handler.sqs:
                use_sqs = True

        use_efs = False
        efs_local_mount_path = ""
        if ac.vpc and ac.vpc.efs:
            use_efs = True
            efs_local_mount_path = ac.vpc.efs.local_mount_path

        django_ctx = None
        if ac.django:
            django_ctx = DjangoContext.from_settings(ac.django, root=root)

        return cls(
            vpc=vpc_ctx,
            secrets=secrets_ctx,
            dockerfile_path=ac.dockerfile_path,
            envs=ac.envs,
            platform=ac.platform,
            django=django_ctx,
            region=root.region,
            slug=root.slug,
            stage=root.stage,
            namespace=root.namespace,
            resource_prefix=resource_prefix,
            handlers=handlers,
            ecr_name=resource_prefix + "lambda",
            use_s3=root.s3 is not None,
            use_route53=use_route53,
            use_sqs=use_sqs,
            use_efs=use_efs,
            permissions_boundary=ac.permissions_boundary,
            efs_local_mount_path=efs_local_mount_path,
        )


class NeonContext(BaseModel):
    pg_version: int = 15
    api_key: str | None = None
    project_name: str
    branch_name: str
    name: str
    role_name: str
    region_id: str

    @classmethod
    def from_settings(cls, neon: settings.Neon, root: settings.Settings) -> NeonContext:
        return cls(
            pg_version=neon.pg_version,
            api_key=neon.api_key,
            project_name=neon.project_name,
            branch_name=root.stage,
            name=root.project_name,
            role_name=root.project_name,
            region_id="aws-" + root.region,
        )


class TiDbContext(BaseModel):
    public_key: str | None = None
    private_key: str | None = None
    tidb_project: str | None = None
    cluster_name: str
    database_name: str
    region: str
    project_name: str

    @classmethod
    def from_settings(cls, tidb: settings.TiDb, root: settings.Settings) -> TiDbContext:
        cluster_name = tidb.cluster or re.sub(
            r"-{2,}", "-", re.sub(r"[^a-zA-Z0-9]", "-", root.project_name)
        ).strip("-")
        database_name = f"{root.project_name}_{root.stage}".replace("-", "_")
        return cls(
            public_key=tidb.public_key,
            private_key=tidb.private_key,
            tidb_project=tidb.project,
            cluster_name=cluster_name,
            database_name=database_name,
            region=tidb.region,
            project_name=root.project_name,
        )


class S3Context(BaseModel):
    region: str
    bucket_name: str
    public_dirs: list[str] = []

    @classmethod
    def from_settings(cls, s3: settings.S3, root: settings.Settings) -> S3Context:
        format_vars = {
            "namespace": root.namespace,
            "stage": root.stage,
            "project": root.project_name,
        }
        return cls(
            region=root.region,
            bucket_name=s3.bucket_name_format.format(**format_vars),
            public_dirs=s3.public_dirs,
        )


class RedirectFromContext(BaseModel):
    domain: str
    hosted_zone_id_override: str | None = None
    region: Literal["us-east-1"] = "us-east-1"

    @computed_field
    @property
    def yaml_key(self) -> str:
        return "".join([s.capitalize() for s in self.domain.split(".")])

    @computed_field
    @cached_property
    def bucket_website_domain(self) -> str:
        if self.region != "us-east-1":
            raise Exception("Never reach here because of context validation")
        return f"{self.domain}.s3-website-us-east-1.amazonaws.com"

    @computed_field
    @cached_property
    def hosted_zone_id(self) -> str | None:
        if self.hosted_zone_id_override:
            return self.hosted_zone_id_override
        if not self.domain:
            return None
        return get_hosted_zone_id_from_domain(self.domain)

    @classmethod
    def from_settings(cls, rf: settings.RedirectFrom) -> RedirectFromContext:
        return cls(
            domain=rf.domain,
            hosted_zone_id_override=rf.hosted_zone_id_override,
        )


class RouteContext(BaseModel):
    path_pattern: str = ""
    is_spa: bool = False
    is_versioned: bool = False
    spa_fallback_html: str = "index.html"
    versioned_max_age: int = 60 * 60 * 24 * 365
    ref: str = ""

    @computed_field
    @property
    def name(self) -> str:
        if not self.path_pattern:
            return "root"
        assert self.path_pattern[0] == "/", "Should be validated in settings"
        parts = []
        for part in self.path_pattern.split("/"):
            if part and part != "*":
                alnum_only = "".join(ch for ch in part if ch.isalnum())
                parts.append(alnum_only)
        return "-".join(parts)

    @computed_field
    @property
    def yaml_key(self) -> str:
        return "".join([s.capitalize() for s in self.name.split("-")])

    @computed_field
    @property
    def url_fallback_function_indent8(self) -> str:
        lines = []
        for i, line in enumerate(self._url_fallback_function.splitlines()):
            if i == 0:
                lines.append(line)
            else:
                lines.append(" " * 8 + line)
        return "\n".join(lines)

    @property
    def _url_fallback_function(self):
        return """function handler(event) {
    var request = event.request;
    var lastItem = request.uri.split('/').pop();
    if (!lastItem.includes('.')) {
        request.uri = '%s/%s';
    }
    return request;
}
""" % (self.path_pattern, self.spa_fallback_html)

    @classmethod
    def from_settings(cls, route: settings.Route) -> RouteContext:
        return cls(
            path_pattern=route.path_pattern,
            is_spa=route.is_spa,
            is_versioned=route.is_versioned,
            spa_fallback_html=route.spa_fallback_html,
            versioned_max_age=route.versioned_max_age,
            ref=route.ref,
        )


class CloudFrontContext(BaseModel):
    region: Literal["us-east-1"] = "us-east-1"
    domain: str | None = None
    hosted_zone_id_override: str | None = None
    slug: str
    bucket_name: str
    origin_prefix: str
    resource_prefix: str
    redirect_from: list[RedirectFromContext] = []
    routes: list[RouteContext] = []

    @computed_field
    @property
    def yaml_key(self) -> str:
        return "".join([s.capitalize() for s in self.slug.split("-")])

    @computed_field
    @property
    def default_route(self) -> RouteContext:
        for route in self.routes:
            if route.path_pattern == "":
                return route
        raise Exception("default route should be defined")

    def get_route(self, ref: str) -> RouteContext:
        for route in self.routes:
            if route.ref == ref:
                return route
        raise ValueError(f"route ref [{ref}] not found")

    @computed_field
    @property
    def extra_routes(self) -> list[RouteContext]:
        return [route for route in self.routes if route.path_pattern != ""]

    @computed_field
    @cached_property
    def hosted_zone_id(self) -> str | None:
        if not self.domain:
            return None
        if self.hosted_zone_id_override:
            return self.hosted_zone_id_override
        return get_hosted_zone_id_from_domain(self.domain)

    @classmethod
    def from_settings(
        cls, cf: settings.CloudFront, root: settings.Settings
    ) -> CloudFrontContext:
        assert root.s3, "s3 is required when cloudfront is configured"
        format_vars = {
            "namespace": root.namespace,
            "stage": root.stage,
            "project": root.project_name,
        }
        resource_prefix = root.prefix_template.format(
            stage=root.stage,
            project=root.project_name,
            namespace=root.namespace,
        )
        return cls(
            domain=cf.domain,
            hosted_zone_id_override=cf.hosted_zone_id_override,
            slug=root.slug,
            bucket_name=root.s3.bucket_name_format.format(**format_vars),
            origin_prefix=cf.origin_prefix,
            resource_prefix=resource_prefix,
            redirect_from=[
                RedirectFromContext.from_settings(rf) for rf in cf.redirect_from
            ],
            routes=[RouteContext.from_settings(r) for r in cf.routes],
        )


class Context(BaseModel):
    general: GeneralContext | None = None
    awscontainer: AwsContainerContext | None = None
    neon: NeonContext | None = None
    tidb: TiDbContext | None = None
    s3: S3Context | None = None
    cloudfront: CloudFrontContext | None = None
    project_name: str
    stage: str

    @classmethod
    def from_settings(cls, s: settings.Settings) -> Context:
        general_ctx = GeneralContext.from_general_settings(s.general)

        awscontainer_ctx = None
        if s.awscontainer:
            awscontainer_ctx = AwsContainerContext.from_settings(s.awscontainer, s)

        neon_ctx = None
        if s.neon:
            neon_ctx = NeonContext.from_settings(s.neon, s)

        tidb_ctx = None
        if s.tidb:
            tidb_ctx = TiDbContext.from_settings(s.tidb, s)

        s3_ctx = None
        if s.s3:
            s3_ctx = S3Context.from_settings(s.s3, s)

        cloudfront_ctx = None
        if s.cloudfront:
            cloudfront_ctx = CloudFrontContext.from_settings(s.cloudfront, s)

        return cls(
            general=general_ctx,
            awscontainer=awscontainer_ctx,
            neon=neon_ctx,
            tidb=tidb_ctx,
            s3=s3_ctx,
            cloudfront=cloudfront_ctx,
            project_name=s.project_name,
            stage=s.stage,
        )

    @classmethod
    def from_toml(cls, *, stage: str, path: str | Path | None = None):
        path = path or get_toml_path()
        return cls.from_settings(settings.Settings.from_toml(stage=stage, path=path))

    @model_validator(mode="after")
    def check_django(self):
        if self.awscontainer and self.awscontainer.django:
            for _, storage in self.awscontainer.django.storages.items():
                if storage.store == "s3" and not self.s3:
                    raise ValueError("s3 is required for s3 storage")
            for _, cache in self.awscontainer.django.caches.items():
                if cache.store == "efs" and not (
                    self.awscontainer.vpc and self.awscontainer.vpc.efs
                ):
                    raise ValueError("vpc is required for efs cache")
        return self
