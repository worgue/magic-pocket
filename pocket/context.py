from __future__ import annotations

import re
from functools import cached_property
from typing import Literal

from pydantic import BaseModel, computed_field, model_validator

from . import settings
from .django.context import DjangoContext
from .general_context import GeneralContext, VpcContext
from .resources.aws.secretsmanager import PocketSecretIsNotReady, SecretsManager
from .resources.aws.ssm import SsmStore
from .settings import (
    BuildBackend,
    BuildConfig,
    ManagedSecretSpec,
    StoreType,
    UserSecretSpec,
)
from .utils import echo, get_hosted_zone_id_from_domain


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
    export_api_domain: str | None = None

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


class BuildContext(BaseModel):
    backend: BuildBackend = "codebuild"
    compute_type: str = "BUILD_GENERAL1_MEDIUM"
    depot_project_id: str | None = None

    @classmethod
    def from_settings(cls, build: BuildConfig) -> BuildContext:
        return cls(
            backend=build.backend,
            compute_type=build.compute_type,
            depot_project_id=build.depot_project_id,
        )


class AwsContainerContext(BaseModel):
    vpc: VpcContext | None = None
    secrets: SecretsContext | None = None
    dockerfile_path: str
    envs: dict[str, str] = {}
    signing_key_imports: dict[str, str] = {}
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
    build: BuildContext = BuildContext()

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
            build=BuildContext.from_settings(ac.build),
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


class RdsContext(BaseModel):
    vpc: VpcContext
    min_capacity: float = 0.5
    max_capacity: float = 2.0
    region: str
    cluster_identifier: str
    instance_identifier: str
    database_name: str
    master_username: str = "postgres"
    subnet_group_name: str
    security_group_name: str
    slug: str

    @classmethod
    def from_settings(cls, rds: settings.Rds, root: settings.Settings) -> RdsContext:
        assert rds.vpc, "rds.vpc must be resolved by resolve_vpc"
        vpc_ctx = VpcContext.from_settings(rds.vpc, root.general)
        resource_prefix = root.prefix_template.format(
            stage=root.stage,
            project=root.project_name,
            namespace=root.namespace,
        )
        database_name = f"{root.project_name}_{root.stage}".replace("-", "_")
        return cls(
            vpc=vpc_ctx,
            min_capacity=rds.min_capacity,
            max_capacity=rds.max_capacity,
            region=root.region,
            cluster_identifier=f"{resource_prefix}aurora",
            instance_identifier=f"{resource_prefix}aurora-1",
            database_name=database_name,
            subnet_group_name=f"{resource_prefix}aurora",
            security_group_name=f"{resource_prefix}aurora-rds",
            slug=root.slug,
        )


class S3Context(BaseModel):
    region: str
    bucket_name: str

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
    type: Literal["s3", "api"] = "s3"
    handler: str | None = None
    path_pattern: str = ""
    is_default: bool = False
    is_spa: bool = False
    is_versioned: bool = False
    spa_fallback_html: str = "index.html"
    versioned_max_age: int = 60 * 60 * 24 * 365
    ref: str = ""
    signed: bool = False
    build: str | None = None
    build_dir: str | None = None
    origin_path: str = ""
    require_token: bool = False
    login_path: str = "/api/auth/login"

    @computed_field
    @property
    def is_api(self) -> bool:
        return self.type == "api"

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
            type=route.type,
            handler=route.handler,
            path_pattern=route.path_pattern,
            is_default=route.is_default,
            is_spa=route.is_spa,
            is_versioned=route.is_versioned,
            spa_fallback_html=route.spa_fallback_html,
            versioned_max_age=route.versioned_max_age,
            ref=route.ref,
            signed=route.signed,
            build=route.build,
            build_dir=route.build_dir,
            origin_path=route.origin_path or "",
            require_token=route.require_token,
            login_path=route.login_path,
        )


class CloudFrontContext(BaseModel):
    name: str
    region: Literal["us-east-1"] = "us-east-1"
    s3_region: str
    domain: str | None = None
    hosted_zone_id_override: str | None = None
    slug: str
    bucket_name: str
    resource_prefix: str
    redirect_from: list[RedirectFromContext] = []
    routes: list[RouteContext] = []
    signing_key: str | None = None
    token_secret: str | None = None
    api_origins: dict[str, str] = {}

    @computed_field
    @property
    def yaml_key(self) -> str:
        return "".join([s.capitalize() for s in self.slug.split("-")])

    @computed_field
    @property
    def bucket_policy_prefix(self) -> str:
        prefixes = [
            r.origin_path for r in self.routes if not r.is_api and r.origin_path
        ]
        if not prefixes:
            return ""
        parts_list = [p.split("/") for p in prefixes]
        common: list[str] = []
        for segments in zip(*parts_list, strict=False):
            if len(set(segments)) == 1:
                common.append(segments[0])
            else:
                break
        return "/".join(common)

    @computed_field
    @property
    def default_route(self) -> RouteContext:
        for route in self.routes:
            if route.is_default:
                return route
        raise Exception("default route should be defined")

    def get_route(self, ref: str) -> RouteContext:
        for route in self.routes:
            if route.ref == ref:
                return route
        raise ValueError(f"route ref [{ref}] not found")

    @computed_field
    @property
    def uploadable_routes(self) -> list[RouteContext]:
        return [r for r in self.routes if r.build_dir]

    @computed_field
    @property
    def extra_routes(self) -> list[RouteContext]:
        return [
            route for route in self.routes if not route.is_default and not route.is_api
        ]

    @computed_field
    @property
    def api_routes(self) -> list[RouteContext]:
        return [route for route in self.routes if route.is_api]

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
        cls, cf: settings.CloudFront, root: settings.Settings, *, name: str
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
            name=name,
            s3_region=root.region,
            domain=cf.domain,
            hosted_zone_id_override=cf.hosted_zone_id_override,
            slug=f"{root.slug}-{name}",
            bucket_name=root.s3.bucket_name_format.format(**format_vars),
            resource_prefix=resource_prefix,
            redirect_from=[
                RedirectFromContext.from_settings(rf) for rf in cf.redirect_from
            ],
            routes=[RouteContext.from_settings(r) for r in cf.routes],
            signing_key=cf.signing_key,
            token_secret=cf.token_secret,
        )


class Context(BaseModel):
    general: GeneralContext | None = None
    awscontainer: AwsContainerContext | None = None
    neon: NeonContext | None = None
    tidb: TiDbContext | None = None
    rds: RdsContext | None = None
    s3: S3Context | None = None
    cloudfront: dict[str, CloudFrontContext] = {}
    project_name: str
    stage: str

    @staticmethod
    def _build_api_origins(
        slug: str,
        awscontainer_ctx: AwsContainerContext,
        cloudfront_ctx: dict[str, CloudFrontContext],
    ) -> None:
        """API route の handler → CFn Export名 のマッピングを構築する"""
        for cf_name, cf_ctx in cloudfront_ctx.items():
            api_origins: dict[str, str] = {}
            for route in cf_ctx.routes:
                if not (route.is_api and route.handler):
                    continue
                export_name = f"{slug}-{route.handler}-api-domain"
                api_origins[route.handler] = export_name
                if route.handler in awscontainer_ctx.handlers:
                    handler_ctx = awscontainer_ctx.handlers[route.handler]
                    if not handler_ctx.export_api_domain:
                        awscontainer_ctx.handlers[route.handler] = (
                            handler_ctx.model_copy(
                                update={"export_api_domain": export_name}
                            )
                        )
            if api_origins:
                cloudfront_ctx[cf_name] = cf_ctx.model_copy(
                    update={"api_origins": api_origins}
                )

    @classmethod
    def _apply_cloudfront_cross_refs(
        cls,
        s: settings.Settings,
        awscontainer_ctx: AwsContainerContext,
        cloudfront_ctx: dict[str, CloudFrontContext],
    ) -> AwsContainerContext:
        """CloudFront ↔ AwsContainer のクロススタック参照を構築する"""
        cls._build_api_origins(s.slug, awscontainer_ctx, cloudfront_ctx)

        # signing_key_imports: CloudFrontKeys の Export を Lambda 環境変数に
        if s.awscontainer and s.awscontainer.secrets:
            signing_key_imports: dict[str, str] = {}
            managed = s.awscontainer.secrets.managed
            for _cf_name, cf_ctx in cloudfront_ctx.items():
                if cf_ctx.signing_key and cf_ctx.signing_key in managed:
                    spec = managed[cf_ctx.signing_key]
                    id_suffix = spec.options.get("id_environ_suffix", "_ID")
                    env_var_name = cf_ctx.signing_key + str(id_suffix)
                    export_name = f"{cf_ctx.slug}-public-key-id"
                    signing_key_imports[env_var_name] = export_name
            if signing_key_imports:
                awscontainer_ctx = awscontainer_ctx.model_copy(
                    update={"signing_key_imports": signing_key_imports}
                )
        return awscontainer_ctx

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

        rds_ctx = None
        if s.rds:
            rds_ctx = RdsContext.from_settings(s.rds, s)

        s3_ctx = None
        if s.s3:
            s3_ctx = S3Context.from_settings(s.s3, s)

        cloudfront_ctx: dict[str, CloudFrontContext] = {}
        for name, cf in s.cloudfront.items():
            cloudfront_ctx[name] = CloudFrontContext.from_settings(cf, s, name=name)

        if awscontainer_ctx:
            awscontainer_ctx = cls._apply_cloudfront_cross_refs(
                s, awscontainer_ctx, cloudfront_ctx
            )

        return cls(
            general=general_ctx,
            awscontainer=awscontainer_ctx,
            neon=neon_ctx,
            tidb=tidb_ctx,
            rds=rds_ctx,
            s3=s3_ctx,
            cloudfront=cloudfront_ctx,
            project_name=s.project_name,
            stage=s.stage,
        )

    @classmethod
    def from_toml(cls, *, stage: str):
        return cls.from_settings(settings.Settings.from_toml(stage=stage))

    @model_validator(mode="after")
    def check_django(self):
        if self.awscontainer and self.awscontainer.django:
            for key, storage in self.awscontainer.django.storages.items():
                if storage.store == "s3" and not self.s3:
                    raise ValueError("s3 is required for s3 storage")
                if storage.distribution and storage.distribution not in self.cloudfront:
                    raise ValueError(
                        f"storage '{key}': distribution '{storage.distribution}' "
                        f"not found in cloudfront"
                    )
            for _, cache in self.awscontainer.django.caches.items():
                if cache.store == "efs" and not (
                    self.awscontainer.vpc and self.awscontainer.vpc.efs
                ):
                    raise ValueError("vpc is required for efs cache")
        return self
