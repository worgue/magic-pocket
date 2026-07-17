from __future__ import annotations

import sys
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .django.settings import Django
from .general_settings import GeneralSettings, Vpc
from .utils import camel_logical_name, echo, get_toml_path, route_logical_name

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def _deep_merge(base: dict, override: dict) -> dict:
    """dict を再帰的に deep merge する。list やスカラーは上書き (replace)。"""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


# Restrict string to a valid environment variable name
EnvStr = Annotated[str, Field(pattern="^[a-zA-Z0-9_]+$")]

# Restrict string to a valid Docker tag
TagStr = Annotated[str, Field(pattern="^[a-z0-9][a-z0-9._-]*$", max_length=128)]

# Formatted string
FormatStr = Annotated[
    str,
    Field(
        pattern="^[a-z0-9{][{}a-z0-9._-]*$",
        max_length=128,
        description=(
            "Formatted string. You can use variables: "
            "namespace, project, stage(for containers), and ref(for vpc)\n"
            "e.g) {stage}-{project}-{namespace}"
        ),
    ),
]

StoreType = Literal["sm", "ssm"]

# DB/KVS リソースの provisioning 方式。
#   "deploy"  : deploy が当該リソースを ensure し接続 URL を供給する (zero-config)。
#   "command" : deploy は当該リソースに触れない (管理 API 非依存 / credential 不要)。
#               provisioning は `pocket <db> store-url` に一任し、deploy は
#               stored-read のみ。
ProvisioningMode = Literal["deploy", "command"]

BuildBackend = Literal["codebuild", "docker", "depot"]


def _reject_skip_check_existing(data, *, resource: str):
    """削除済み設定 `skip_check_existing` が残っていたら fail-fast で移行を促す。

    `provisioning = "command"` + `pocket <db> store-url` への移行で廃止した。
    extra="ignore" だと黙殺されるため model_validator(mode="before") で raw 入力を
    検査する。
    """
    if isinstance(data, dict) and "skip_check_existing" in data:
        raise ValueError(
            "[%s] skip_check_existing は廃止されました。"
            '`provisioning = "command"` に置き換え、接続 URL を '
            "[awscontainer.secrets.user] の type で宣言したうえで deploy 前に "
            "`pocket resource %s store-url --stage <stage>` を実行してください。"
            % (resource, resource)
        )
    return data


class BuildConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: BuildBackend = "codebuild"
    compute_type: str = "BUILD_GENERAL1_MEDIUM"
    depot_project_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def accept_string(cls, data):
        if isinstance(data, str):
            return {"backend": data}
        return data


class ManagedSecretSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal[
        "auto_database_url",
        "password",
        "neon_database_url",
        "tidb_database_url",
        "rds_database_url",
        "upstash_redis_url",
        "rsa_pem_base64",
        "cloudfront_signing_key",
        "spa_token_secret",
        "origin_verify_secret",
    ]
    options: dict[str, str | int] = {}
    # Used in mediator
    # PasswordOptions:
    #     length: int
    # Used in runtime
    # RsaPemBase64Options:
    #     pem_base64_environ_suffix: str = "_PEM_BASE64"
    #     pub_base64_environ_suffix: str = "_PUB_BASE64"
    # CloudFrontSigningKeyOptions:
    #     pem_base64_environ_suffix: str = "_PEM_BASE64"
    #     pub_base64_environ_suffix: str = "_PUB_BASE64"
    #     id_environ_suffix: str = "_ID"


# user secret の stored mode 用 type。
# managed の computed type と同名だが、user 側に置くと「事前 provision 済みの URL を
# 参照するだけ (stored)」を意味する。pocket は API を一切叩かず、導出した正準名の secret
# を読むだけ。対象は deploy 時に管理 API key を要求する neon / tidb / upstash (rds は
# runtime 構築方式で stored 化の旨味が無く rotation 追従を壊すため対象外)。
UserSecretType = Literal["neon_database_url", "tidb_database_url", "upstash_redis_url"]


class UserSecretSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # name と type は排他 (name = 任意名を明示参照 / type = stored mode で正準名を導出)
    name: str | None = None  # SM: ARN or secret name, SSM: parameter name/path
    type: UserSecretType | None = None
    store: StoreType | None = None  # Noneの場合Secrets.storeを継承

    @model_validator(mode="after")
    def check_name_or_type(self):
        # 「どちらも無し」を禁止。「両方指定」(name+type 排他) はユーザー入力時点で
        # Secrets.check_user_name_type_exclusive が弾く。ここで両方を禁止しないのは、
        # stored mode の解決後 (from_settings) に type を保持したまま導出 name を
        # 埋めた内部状態が再バリデーションを通る必要があるため。
        if self.name is None and self.type is None:
            raise ValueError(
                "UserSecretSpec requires 'name' or 'type' "
                "(name = 明示参照 / type = stored mode で正準名を自動導出)."
            )
        return self


class Secrets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store: StoreType = "sm"
    pocket_key_format: Annotated[
        FormatStr,
        Field(
            description=(
                "Format string for pocket key. e.g) {stage}-{project}-{namespace}\n"
                "You can use variables: namespace, project, stage\n"
                "Although default value contains stage and project, "
                "it is not required. Because the secret value is stored "
                "under the stage and project key in json.\n"
                "If you remove stage or project from the key, be careful "
                "not to generate secret keys simaltaneously in different situations.\n"
                "It might cause a race condition."
            )
        ),
    ] = "{stage}-{project}-{namespace}"
    managed: Annotated[
        dict[EnvStr, ManagedSecretSpec],
        Field(
            description=(
                "These secrets are managed by magic-pocket, "
                "magic-pocket create secrets when creating lambda container."
            )
        ),
    ] = {}
    user: Annotated[
        dict[EnvStr, UserSecretSpec],
        Field(
            description=(
                "These secrets get GetSecretValue/GetParameter permissions "
                "automatically based on their store type.\n"
                "You still need to create them by yourself."
            )
        ),
    ] = {}
    extra_resources: Annotated[
        list[str],
        Field(
            description=(
                "List secret ARNs to allow GetSecretValue/GetParameter, "
                "if you want to access them from your own lambda functions.\n"
                "Supports both SM and SSM ARNs."
            )
        ),
    ] = []
    require_list_secrets: bool = False

    @model_validator(mode="after")
    def check_user_name_type_exclusive(self):
        for key, spec in self.user.items():
            if spec.name is not None and spec.type is not None:
                raise ValueError(
                    "user secret '%s': 'name' と 'type' は排他です "
                    "(name = 明示参照 / type = stored mode のどちらか一方のみ)." % key
                )
        return self

    @model_validator(mode="after")
    def check_user_type_unique(self):
        # stored mode の保存パスは type 基準 (/{pocket_key}-user/{type}) で導出する
        # ため、同一 type の user secret が複数あると保存先が衝突する。1 stage に
        # つき type は 1 個までに制限する。
        seen: dict[str, str] = {}
        for key, spec in self.user.items():
            if spec.type is None:
                continue
            if spec.type in seen:
                raise ValueError(
                    "user secret '%s' と '%s' が同一 type=%s です。stored の保存パスは "
                    "type 基準で導出されるため、1 stage につき同一 type は 1 個までに "
                    "してください。" % (key, seen[spec.type], spec.type)
                )
            seen[spec.type] = key
        return self


class AwsContainerIam(BaseModel):
    """Lambda execution role に追加注入する IAM 設定。

    built-in な service flag (`use_s3` 等) や `secrets.allowed_*_resources` で
    カバーできない権限を、ユーザーが宣言的に与えるための逃げ道。
    """

    model_config = ConfigDict(extra="forbid")

    managed_policy_arns: list[str] = []
    """LambdaRole の ManagedPolicyArns に追加する AWS managed policy ARN の list。"""

    inline_policies: dict[str, dict] = {}
    """LambdaRole の Policies に追加する inline policy。

    key は PolicyName の suffix (resource_prefix が前置される)、
    value は PolicyDocument の dict (Version / Statement を含む)。
    """


class AwsContainer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vpc: Vpc | None = None
    secrets: Secrets | None = None
    handlers: dict[str, LambdaHandler] = {}
    dockerfile_path: str
    envs: dict[str, str] = {}
    platform: str = "linux/amd64"
    django: Django | None = None
    permissions_boundary: str | None = None
    iam: AwsContainerIam = AwsContainerIam()
    build: BuildConfig = BuildConfig()
    # ECR repository 名の上書き。省略時は resource_prefix + "lambda" を使う。
    # 同一 AWS アカウント内で複数 stage が同じ repo を共有したい場合に指定する
    # (build once + commit-hash 昇格で再ビルドなし deploy を成立させるため)。
    ecr_name: str | None = None

    @model_validator(mode="after")
    def check_handlers(self):
        check_command = "pocket.django.lambda_handlers.management_command_handler"
        commend_list = [h for h in self.handlers.values() if h.command == check_command]
        if 1 < len(commend_list):
            raise ValueError("Only one management command handler is allowed.")
        return self


class LambdaHandler(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str
    timeout: int = 30
    memory_size: int = 512
    reserved_concurrency: int | None = None
    apigateway: ApiGateway | None = None
    sqs: Sqs | None = None


class ApiGateway(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str | None = None
    create_records: bool = True
    hosted_zone_id_override: str | None = None


_LAMBDA_SCHEDULER = "pocket.lambda_scheduler"
_DJANGO_MANAGEMENT_SCHEDULER = "pocket.django.management_lambda_scheduler"
_BUILTIN_SCHEDULERS = (_LAMBDA_SCHEDULER, _DJANGO_MANAGEMENT_SCHEDULER)


class _ScheduleEntryBase(BaseModel):
    # 派生の LambdaScheduleEntry / DjangoManagementScheduleEntry にも継承される
    model_config = ConfigDict(extra="forbid")

    cron: str | None = None
    rate: str | None = None
    handler: str

    @model_validator(mode="after")
    def check_cron_or_rate(self):
        if bool(self.cron) == bool(self.rate):
            raise ValueError("schedule entry must specify exactly one of cron / rate")
        return self

    @computed_field
    @property
    def schedule_expression(self) -> str:
        if self.cron:
            return f"cron({self.cron})"
        if not self.rate:
            raise RuntimeError("rate must be set (validated by check_cron_or_rate)")
        return f"rate({self.rate})"


class LambdaScheduleEntry(_ScheduleEntryBase):
    """汎用 Lambda 向け scheduler entry。任意の input dict をそのまま渡す。"""

    scheduler: Literal["pocket.lambda_scheduler"] = "pocket.lambda_scheduler"
    input: dict = {}


class DjangoManagementScheduleEntry(_ScheduleEntryBase):
    """Django management command を呼び出すショートカット scheduler entry。

    handler は management_command_handler を指す必要がある。
    Lambda には {"manage": "<shell-style command line>"} が渡され、
    handler 側で shlex.split + call_command が行われる。
    """

    scheduler: Literal["pocket.django.management_lambda_scheduler"]
    manage: str

    @model_validator(mode="after")
    def check_manage_not_empty(self):
        if not self.manage.strip():
            raise ValueError("manage must be a non-empty shell-style command")
        return self


ScheduleEntry = Annotated[
    LambdaScheduleEntry | DjangoManagementScheduleEntry,
    Field(discriminator="scheduler"),
]


class Scheduler(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schedules: dict[str, ScheduleEntry] = {}

    @model_validator(mode="before")
    @classmethod
    def normalize_entries(cls, data):
        """schedules entry に scheduler が省略されている場合 default を補う。

        discriminated union は discriminator が無いと validation できないため、
        scheduler フィールドが未指定なら lambda_scheduler を埋める。
        """
        if not isinstance(data, dict):
            return data
        schedules = data.get("schedules")
        if not isinstance(schedules, dict):
            return data
        for entry in schedules.values():
            if isinstance(entry, dict) and "scheduler" not in entry:
                entry["scheduler"] = _LAMBDA_SCHEDULER
        return data


class Sqs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_size: int = 10
    message_retention_period: int = 345600
    # minimum 2
    # https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-properties-lambda-eventsourcemapping-scalingconfig.html#aws-properties-lambda-eventsourcemapping-scalingconfig-properties
    maximum_concurrency: int = 2
    # set the maxReceiveCount on the source queue's redrive policy to at least 5
    # https://docs.aws.amazon.com/lambda/latest/dg/with-sqs.html#events-sqs-queueconfig
    dead_letter_max_receive_count: int = 5
    dead_letter_message_retention_period: int = 1209600
    report_batch_item_failures: bool = True


class Neon(BaseSettings):
    project_name: str
    pg_version: int = 15
    # 省略時は project の default ブランチ (通常 main) を使う。stage 名との暗黙結合を
    # 避けるため。per-stage で上書きしたい場合は [<stage>.neon] に branch_name を書く
    # (stage override は from_toml の merge_stage_data で [neon] に deep-merge される)。
    # FormatStr なので {stage}/{project}/{namespace} を展開可能。動的な feature 環境で
    # 環境ごとに別ブランチを使う場合は branch_name = "{stage}" のように書く。
    branch_name: FormatStr | None = None
    # branch_name のブランチを新規作成する際の親ブランチ名。省略時は parent_id を送らず
    # Neon が project の default ブランチ (= main) から枝分かれさせる (= 現状の挙動)。
    # 非 default ブランチを親にしたい時だけ指定する (例: feature を別 stage から分岐)。
    # branch_name 同様 FormatStr。既存ブランチがあれば作成は走らないので無視される。
    parent_branch_name: FormatStr | None = None
    api_key: str | None = Field(alias="neon_api_key", default=None)
    # "deploy" (既定): deploy が branch/role/db を ensure し DATABASE_URL を供給する。
    # "command": deploy は Neon に一切触れない (credential 不要)。provisioning は
    #            `pocket neon store-url` に一任し、deploy は stored user secret を
    #            読むだけ。
    provisioning: ProvisioningMode = "deploy"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_skip_check_existing(cls, data):
        return _reject_skip_check_existing(data, resource="neon")


class TiDb(BaseSettings):
    public_key: str | None = Field(alias="tidb_public_key", default=None)
    private_key: str | None = Field(alias="tidb_private_key", default=None)
    project: str | None = None
    cluster: str | None = None
    region: str = "ap-northeast-1"
    # Neon.provisioning と同義。"command" で deploy が TiDB に触れない。
    provisioning: ProvisioningMode = "deploy"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_skip_check_existing(cls, data):
        return _reject_skip_check_existing(data, resource="tidb")


class Upstash(BaseSettings):
    email: str | None = Field(alias="upstash_email", default=None)
    api_key: str | None = Field(alias="upstash_api_key", default=None)
    budget: int = 20
    # Neon.provisioning と同義。"command" で deploy が Upstash に触れない
    # (credential 不要)。provisioning は `pocket resource upstash store-url` に
    # 一任し、deploy は stored read のみ。
    provisioning: ProvisioningMode = "deploy"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_skip_check_existing(cls, data):
        return _reject_skip_check_existing(data, resource="upstash")


class Dsql(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deletion_protection: bool = False


class Rds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    managed: bool = True
    vpc: Vpc | None = None  # resolve_vpc で解決
    min_capacity: float = 0.5
    max_capacity: float = 2.0
    snapshot_identifier: str | None = None
    # DB 名の上書き。未指定なら "{stage}_{project}" (他リソース名の
    # {stage}-{project} 順に合わせる)。既存クラスタ参照や snapshot 復元
    # (RestoreDBClusterFromSnapshot は DatabaseName を無視し、元の DB 名が
    # そのまま残る) で、pocket が実 DB 名を指す必要がある場合に指定する。
    database: str | None = None
    # マスターパスワードの管理方式:
    #   "aws-managed": ManageMasterUserPassword=True。RDS が生成し 7 日周期で自動
    #                  ローテーション (既定。互換)。
    #   "static":      pocket がパスワードを生成し自前 secret に保存。ローテーション
    #                  しない。RDS Proxy 無しでローテーション時ダウンタイムを避けたい
    #                  環境向け。
    password_strategy: Literal["aws-managed", "static"] = "aws-managed"  # noqa: S105 戦略名であって secret 値ではない
    # managed = false (既存参照モード) 用フィールド
    secret_arn: str | None = None
    security_group_id: str | None = None

    @model_validator(mode="after")
    def check_managed_mode(self):
        unmanaged_fields = {
            "secret_arn": self.secret_arn,
            "security_group_id": self.security_group_id,
        }
        has_unmanaged = any(v is not None for v in unmanaged_fields.values())
        managed_fields = {
            "min_capacity (非デフォルト)": self.min_capacity != 0.5,
            "max_capacity (非デフォルト)": self.max_capacity != 2.0,
            "snapshot_identifier": self.snapshot_identifier is not None,
            "database": self.database is not None,
        }
        has_managed_custom = any(managed_fields.values())

        if self.managed and has_unmanaged:
            set_fields = [k for k, v in unmanaged_fields.items() if v is not None]
            raise ValueError(
                f"{', '.join(set_fields)} は managed = false でのみ使用できます。"
                " 既存の RDS を参照する場合は managed = false を指定してください。"
            )
        if not self.managed:
            if self.secret_arn is None:
                raise ValueError("managed = false の場合、secret_arn は必須です。")
            if self.security_group_id is None:
                raise ValueError(
                    "managed = false の場合、security_group_id は必須です。"
                )
            if self.password_strategy != "aws-managed":  # noqa: S105 戦略名であって secret 値ではない
                raise ValueError(
                    "password_strategy は managed = true でのみ使用できます。"
                    " 既存 RDS 参照時のパスワードは secret_arn の secret に従います。"
                )
            if has_managed_custom:
                set_fields = [k for k, v in managed_fields.items() if v]
                raise ValueError(
                    f"managed = false では {', '.join(set_fields)} は 使用できません。"
                )
        return self


class Ses(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_email: str
    region: str | None = None  # None → general.region を継承
    configuration_set: str | None = None


class S3Cors(BaseModel):
    model_config = ConfigDict(extra="forbid")

    methods: list[str]
    cloudfront: str | list[str]


class S3LifecycleRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    prefix: str
    noncurrent_version_expiration_days: int = Field(ge=1)


class S3(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bucket_name_format: FormatStr = "{stage}-{project}-{namespace}"
    cors: S3Cors | None = None
    versioning: bool = False
    lifecycle_rules: list[S3LifecycleRule] = []

    def bucket_name(self, format_vars: dict[str, str]) -> str:
        """bucket_name_format を format_vars で展開したバケット名。

        S3Context と CloudFrontContext が別個に同じ導出を持っていたのを
        この 1 メソッドに集約する (Settings.format_vars を渡して使う)。
        """
        return self.bucket_name_format.format(**format_vars)


class RedirectFrom(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str
    hosted_zone_id_override: str | None = None


Versioning = Literal["content_hash", "deploy_hash"]


class Route(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["s3", "lambda"] = "s3"
    handler: str | None = None
    path_pattern: str = ""
    is_default: bool = False
    is_spa: bool = False
    versioning: Versioning | None = None
    spa_fallback_html: str = "index.html"
    versioned_max_age: int = 60 * 60 * 24 * 365
    ref: str = ""
    signed: bool = False
    build: str | None = None
    build_dir: str | None = None
    origin_path: str | None = None
    require_token: bool = False
    login_path: str = "/api/auth/login"

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_is_versioned(cls, data):
        """旧 is_versioned を明示エラーにする (#8 で versioning に統合)"""
        if isinstance(data, dict) and data.get("is_versioned"):
            raise ValueError(
                "is_versioned は廃止されました。"
                'versioning = "content_hash" を使ってください。'
            )
        return data

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_api_type(cls, data):
        """旧 type = "api" の使用を明示エラーにする(#2 での破壊的リネーム)"""
        if isinstance(data, dict) and data.get("type") == "api":
            raise ValueError(
                'type = "api" は廃止されました。type = "lambda" を使ってください。'
                " あわせて旧 api の制約が解除され、is_default = true も含めて"
                " すべての route オプションが利用可能になっています。"
            )
        return data

    @model_validator(mode="after")
    def check_origin_path(self):
        if self.type == "lambda":
            if self.origin_path is not None:
                raise ValueError("type = 'lambda' cannot use origin_path")
        elif not self.origin_path:
            # origin_path 省略 (None / "") 時は S3 の key prefix が path_pattern だけに
            # なる (origin_path も足すと media/media のように二重階層になる)。ただし
            # path_pattern が prefix を持たない catch-all (path_pattern = "" や "/*") は
            # バケット直下に散らばり他 route と衝突するため origin_path 必須のまま。
            if not self.path_pattern.rstrip("/*").lstrip("/"):
                raise ValueError(
                    "S3 route requires `origin_path` when path_pattern has no prefix "
                    "(catch-all). "
                    'Set a prefix like "/spa" to separate from other S3 routes '
                    "(e.g. Django static at /static).\n"
                    '  Example: { type = "s3", path_pattern = "", '
                    'is_default = true, origin_path = "/spa" }'
                )
        else:
            if self.origin_path == "/":
                # 「バケット直下を配信する catch-all」の意図で書かれる。汎用の
                # "must not ends with /" だと、次に origin_path 省略を試して
                # 上の catch-all エラーに当たり、メッセージ間をループするので
                # ここで意図的な非サポートだと明示する (docs: configuration.md)
                raise ValueError(
                    'origin_path = "/" (バケット直下の配信) はサポートしていません。'
                    " pocket は 1 つの S3 バケットを複数 route で共有するため、"
                    "バケット直下に向けると OAC のバケットポリシーがバケット全体の"
                    "許可になり、route を張っていない prefix のオブジェクトまで"
                    "CDN 経由で到達可能になります。\n"
                    "  prefix を与えてください。URL 上のパスは path_pattern で"
                    "決まるため、配信結果は変わりません"
                    ' (例: origin_path = "/spa")。'
                )
            if self.origin_path[0] != "/":
                raise ValueError("origin_path must starts with /")
            if self.origin_path[-1] == "/":
                raise ValueError("origin_path must not ends with /")
        return self

    def double_prefix_advisory(self) -> str | None:
        """origin_path が path_pattern の prefix と一致する場合の助言文。無ければ None。

        origin_path が path_pattern の prefix と一致すると S3 key が二重階層
        (static/static, media/media 等) になる footgun。prefix を持つ path_pattern
        では origin_path 省略で単一 prefix にできるので助言する (省略時挙動は
        check_origin_path の elif 分岐)。

        raise しない advisory なので、pydantic の mode="after" validator
        (再検証のたびに走る) ではなく Settings.from_toml から 1 回だけ
        emit する (`Settings._emit_advisories`)。二重出力を避けるため。
        """
        if self.type != "s3" or not self.origin_path:
            return None
        path_prefix = self.path_pattern.rstrip("/*").lstrip("/")
        if path_prefix and self.origin_path.strip("/") == path_prefix:
            return (
                f'S3 route origin_path = "{self.origin_path}" が '
                f'path_pattern = "{self.path_pattern}" と重複し、S3 key が '
                f"二重 prefix ({path_prefix}/{path_prefix}/...) になります。"
                " origin_path を省略すると単一 prefix "
                f"({path_prefix}/...) になり、S3 を直接操作する運用で"
                " prefix が直感的になります。"
            )
        return None

    @model_validator(mode="after")
    def check_require_token(self):
        if self.require_token and not self.is_spa:
            raise ValueError("require_token=True requires is_spa=True")
        return self

    @model_validator(mode="after")
    def check_lambda_route(self):
        if self.type == "lambda":
            if not self.handler:
                raise ValueError("handler is required when type = 'lambda'")
            if self.is_spa or self.versioning or self.signed or self.require_token:
                raise ValueError(
                    "type = 'lambda' cannot use "
                    "is_spa, versioning, signed, or require_token"
                )
            if self.build or self.build_dir:
                raise ValueError("type = 'lambda' cannot use build or build_dir")
        if self.handler and self.type != "lambda":
            raise ValueError("handler requires type = 'lambda'")
        return self

    @model_validator(mode="after")
    def check_build(self):
        if self.build and not self.build_dir:
            raise ValueError("build_dir is required when build is set")
        return self

    @model_validator(mode="after")
    def check_flags(self):
        if self.is_spa and self.versioning:
            raise ValueError("is_spa と versioning は同時に設定できません")
        return self

    @model_validator(mode="after")
    def check_is_default(self):
        if self.is_default and self.path_pattern:
            raise ValueError("is_default route must have empty path_pattern")
        if not self.is_default and not self.path_pattern:
            raise ValueError(
                "route with empty path_pattern must have is_default = true"
            )
        return self

    @model_validator(mode="after")
    def check_path_pattern(self):
        if self.path_pattern:
            if self.path_pattern[0] != "/":
                raise ValueError("non default path_pattern must starts with /")
            if self.path_pattern[-1] == "/":
                raise ValueError("path_pattern must not ends with /")
        return self

    @model_validator(mode="after")
    def check_ref(self):
        if self.ref:
            if self.path_pattern[-2:] != "/*":
                raise ValueError("When ref is set, path_pattern must end with /*")
        return self


class CloudFrontWaf(BaseModel):
    """CloudFront に attach する WAFv2 WebACL の宣言。

    block を書くだけで us-east-1 に WebACL が作成され、CloudFront distribution
    に attach される。デフォルトは `enable_ip_set = true`、つまり IP allowlist
    モード (許可済み IP 以外は全 block)。`enable_ip_set = false` に倒すと
    IPSet 自体を作らず、managed_rule_groups のみで「許可ベース + 怪しい
    リクエストだけ block」する構成になる。

    `IPSet` の中身 (実際の CIDR 一覧) は `pocket waf ip ...` CLI で投入する
    (toml には IP リテラルを書けない設計; 真実源を CLI 一本に絞ることで
    drift 事故を防ぐ)。
    """

    model_config = ConfigDict(extra="forbid")

    enable_ip_set: bool = True
    managed_rule_groups: list[str] = []

    @model_validator(mode="after")
    def _must_have_some_rule(self):
        if not self.enable_ip_set and not self.managed_rule_groups:
            raise ValueError(
                "[cloudfront.<name>.waf]: enable_ip_set = false の場合は"
                " managed_rule_groups を 1 つ以上指定してください。"
                " どちらも無効だと WebACL が何もしない pass-through 状態に"
                " なり、WAF を attach する意味がありません。"
            )
        return self


class CloudFront(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str | None = None
    hosted_zone_id_override: str | None = None
    redirect_from: list[RedirectFrom] = []
    routes: list[Route] = []
    signing_key: str | None = None
    token_secret: str | None = None
    managed_assets: str | None = None
    waf: CloudFrontWaf | None = None
    # CloudFront → origin に詐称耐性のある client IP / origin 直叩き防止の secret
    # header を一括で有効化する。secret は magic-pocket が自動生成・管理し
    # (managed secret `POCKET_ORIGIN_VERIFY_SECRET`)、CloudFront の origin custom
    # header と Lambda runtime env の両方に同値を供給する。検証 + REMOTE_ADDR 正規化
    # は同梱の `pocket.django.origin_verify.OriginVerifyMiddleware` が行う。
    # (`CloudFront-Viewer-Address` 相当の client IP 転送自体は flag に関係なく
    #  lambda route の CloudFront Function で常時有効。)
    enable_origin_verify: bool = False

    @model_validator(mode="after")
    def check_domain_redirect_from(self):
        if self.domain is None and self.redirect_from:
            raise ValueError("redirect_from requires domain to be set")
        return self

    @model_validator(mode="after")
    def check_logical_id_uniqueness(self):
        """導出される CFn 論理 ID / Origin Id の衝突・空文字を検証する。

        非英数字は除去されるため `/foo-bar/*` と `/foobar/*` は同名になり、
        テンプレートの論理 ID 重複で deploy が失敗する (prefix 重複検査は
        すり抜ける)。redirect_from の domain も同様。
        """
        seen_routes: dict[str, str] = {}
        for route in self.routes:
            name = route_logical_name(route.path_pattern)
            if not name.strip("-"):
                raise ValueError(
                    "path_pattern '%s' から CloudFormation 論理 ID を導出でき"
                    'ません。catch-all は path_pattern = "" (is_default = true) '
                    "を使ってください。" % route.path_pattern
                )
            if name in seen_routes:
                raise ValueError(
                    "routes '%s' と '%s' は同じ CloudFormation 論理 ID '%s' を"
                    "導出します (英数字以外は除去されます)。"
                    "区別可能な path prefix を使ってください。"
                    % (seen_routes[name], route.path_pattern, name)
                )
            seen_routes[name] = route.path_pattern
        seen_domains: dict[str, str] = {}
        for rf in self.redirect_from:
            key = camel_logical_name(rf.domain)
            if key in seen_domains:
                raise ValueError(
                    "redirect_from '%s' と '%s' は同じ CloudFormation 論理 ID "
                    "'%s' を導出します。" % (seen_domains[key], rf.domain, key)
                )
            seen_domains[key] = rf.domain
        return self

    @model_validator(mode="after")
    def check_token_secret(self):
        has_require_token = any(r.require_token for r in self.routes)
        if has_require_token and not self.token_secret:
            raise ValueError(
                "token_secret is required when route has require_token=true"
            )
        return self

    @model_validator(mode="after")
    def check_routes(self):
        if len(self.routes) == 0:
            raise ValueError("routes must have at least one route")
        defaults = [r for r in self.routes if r.is_default]
        if len(defaults) != 1:
            raise ValueError("routes must have exactly one is_default = true route")
        return self

    @model_validator(mode="after")
    def check_route_ref_unique(self):
        refs = [r.ref for r in self.routes if r.ref]
        seen: set[str] = set()
        for ref in refs:
            if ref in seen:
                raise ValueError(
                    f"routes の ref '{ref}' が重複しています。"
                    "ref は route を一意に識別するため、同じ値を複数の route に"
                    "設定することはできません。"
                )
            seen.add(ref)
        return self

    @model_validator(mode="after")
    def check_route_s3_prefix_overlap(self):
        """S3 ルート同士の S3 prefix が重複していないことを検証する。

        一方の prefix がもう一方の prefix の親になっている場合、
        s3 sync --delete 等で意図せずファイルが削除される危険がある。
        """
        # origin_path 省略 (None / "") の S3 route も、path_pattern 由来の prefix で
        # 衝突検査に含める (含めないと空 origin route が別 route と衝突しても
        # 検出できない)。
        s3_routes = [r for r in self.routes if r.type == "s3"]
        prefixes: list[tuple[str, str]] = []
        for route in s3_routes:
            label = route.ref or route.path_pattern or "default"
            origin_path = route.origin_path or ""
            prefix = (origin_path + route.path_pattern.rstrip("/*")).lstrip("/")
            prefixes.append((label, prefix))

        for i, (label_a, prefix_a) in enumerate(prefixes):
            for label_b, prefix_b in prefixes[i + 1 :]:
                if prefix_a == prefix_b:
                    raise ValueError(
                        "ルート '%s' と '%s' の S3 prefix が同一です: '%s'"
                        % (label_a, label_b, prefix_a)
                    )
                if prefix_b.startswith(prefix_a + "/"):
                    raise ValueError(
                        "ルート '%s' の S3 prefix '%s' は"
                        "ルート '%s' の S3 prefix '%s' "
                        "の子パスです。"
                        "s3 sync --delete 等で意図せず"
                        "ファイルが削除される危険があります。"
                        % (label_b, prefix_b, label_a, prefix_a)
                    )
                if prefix_a.startswith(prefix_b + "/"):
                    raise ValueError(
                        "ルート '%s' の S3 prefix '%s' は"
                        "ルート '%s' の S3 prefix '%s' "
                        "の子パスです。"
                        "s3 sync --delete 等で意図せず"
                        "ファイルが削除される危険があります。"
                        % (label_a, prefix_a, label_b, prefix_b)
                    )
        return self


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    general: GeneralSettings
    stage: TagStr
    vpc: Vpc | None = None
    awscontainer: AwsContainer | None = None
    neon: Neon | None = None
    tidb: TiDb | None = None
    upstash: Upstash | None = None
    dsql: Dsql | None = None
    rds: Rds | None = None
    ses: Ses | None = None
    s3: S3 | None = None
    cloudfront: dict[str, CloudFront] = {}
    scheduler: Scheduler | None = None

    @property
    def project_name(self):
        return self.general.project_name

    @property
    def region(self):
        return self.general.region

    @property
    def namespace(self):
        return self.general.namespace

    @property
    def prefix_template(self):
        return self.general.prefix_template

    @property
    def format_vars(self) -> dict[str, str]:
        """FormatStr 展開用の変数一式。

        context.py の各 from_settings が個別に組み立てていた同一 dict を
        一元化したもの。導出規則は Rust 側 (config.rs の format_vars) と
        揃える必要があるため変えないこと (ズレると secret / queue 名が silent に
        食い違う)。
        """
        return {
            "namespace": self.namespace,
            "stage": self.stage,
            "project": self.project_name,
        }

    @property
    def resource_prefix(self) -> str:
        """リソース名の共通 prefix。e.g) dev-myprj-pocket-

        prefix_template を format_vars で展開したもの。context.py の
        AwsContainer / Dsql / Rds / CloudFront が同一計算を重複させていた。
        """
        return self.prefix_template.format(**self.format_vars)

    @computed_field
    @property
    def slug(self) -> str:
        """Identify the environment. e.g) dev-myprj"""
        return "%s-%s" % (self.stage, self.general.project_name)

    def _check_cloudfront_token_secret(self, name: str, cf: CloudFront):
        if not cf.token_secret:
            return
        if not self.awscontainer:
            raise ValueError(
                f"cloudfront.{name}: awscontainer is required when token_secret is set"
            )
        if not self.awscontainer.secrets:
            raise ValueError(
                f"cloudfront.{name}: awscontainer.secrets is required "
                f"when token_secret is set"
            )
        if cf.token_secret not in self.awscontainer.secrets.managed:
            raise ValueError(
                f"cloudfront.{name}: token_secret '{cf.token_secret}' "
                f"not found in awscontainer.secrets.managed"
            )

    def _check_cloudfront_entry(self, name: str, cf: CloudFront):
        for route in cf.routes:
            if route.signed and not cf.signing_key:
                raise ValueError(
                    f"cloudfront.{name}: signing_key is required "
                    f"when route has signed=true"
                )
            if route.type == "lambda":
                if not route.handler:
                    raise ValueError(
                        f"cloudfront.{name}: handler is required "
                        f"when route has type='lambda'"
                    )
                if not self.awscontainer:
                    raise ValueError(
                        f"cloudfront.{name}: awscontainer is required "
                        f"when route has type='lambda'"
                    )
                if route.handler not in self.awscontainer.handlers:
                    raise ValueError(
                        f"cloudfront.{name}: handler '{route.handler}' "
                        f"not found in awscontainer.handlers"
                    )
                handler = self.awscontainer.handlers[route.handler]
                if not handler.apigateway:
                    raise ValueError(
                        f"cloudfront.{name}: handler '{route.handler}' "
                        f"must have apigateway configured for lambda route"
                    )
        self._check_cloudfront_token_secret(name, cf)
        self._check_cloudfront_origin_verify(name, cf)

    def _check_cloudfront_origin_verify(self, name: str, cf: CloudFront):
        if not cf.enable_origin_verify:
            return
        # origin verify は CloudFront → origin (lambda / API GW) の HTTP header で
        # 成立する。保護対象となる lambda route が無い構成では意味がない。
        if not any(route.type == "lambda" for route in cf.routes):
            raise ValueError(
                f"cloudfront.{name}: enable_origin_verify requires at least one "
                f"lambda route (the origin to protect). S3-only distributions are "
                f"already protected by OAC."
            )

    @model_validator(mode="after")
    def check_rds_vpc(self):
        if self.rds and self.rds.vpc:
            if self.rds.vpc.manage and len(self.rds.vpc.zone_suffixes) < 2:
                raise ValueError(
                    "rds requires vpc with at least 2 zone_suffixes "
                    "(DB Subnet Group needs 2+ AZs)"
                )
        return self

    @model_validator(mode="after")
    def check_rds_requires_awscontainer_vpc(self):
        if self.rds and self.rds.vpc:
            if not self.awscontainer or not self.awscontainer.vpc:
                raise ValueError("rds requires awscontainer with VPC")
        return self

    @model_validator(mode="after")
    def check_scheduler_handlers(self):
        if not self.scheduler or not self.scheduler.schedules:
            return self
        if not self.awscontainer:
            raise ValueError("scheduler is configured but awscontainer is missing")
        management_handler_command = (
            "pocket.django.lambda_handlers.management_command_handler"
        )
        for key, entry in self.scheduler.schedules.items():
            if entry.handler not in self.awscontainer.handlers:
                raise ValueError(
                    f"scheduler.schedules.{key}: handler '{entry.handler}' "
                    f"not found in awscontainer.handlers"
                )
            if isinstance(entry, DjangoManagementScheduleEntry):
                handler = self.awscontainer.handlers[entry.handler]
                if handler.command != management_handler_command:
                    raise ValueError(
                        f"scheduler.schedules.{key}: scheduler="
                        f"'{_DJANGO_MANAGEMENT_SCHEDULER}' requires the target "
                        f"handler '{entry.handler}' to use command="
                        f"'{management_handler_command}', "
                        f"got '{handler.command}'"
                    )
        return self

    @model_validator(mode="after")
    def check_cloudfront_requires_s3(self):
        if self.cloudfront and not self.s3:
            raise ValueError("s3 is required when cloudfront is configured")
        for name, cf in self.cloudfront.items():
            self._check_cloudfront_entry(name, cf)
        return self

    @classmethod
    def from_toml(cls, *, stage: str):
        text = get_toml_path().read_text()
        cls.check_generator_version(text)
        data = tomllib.loads(text)
        cls.check_keys(data)
        cls.check_stage(stage, data)
        cls.merge_stage_data(stage, data)
        cls.remove_stages_data(stage, data)
        # stage override 適用後に検証する ([<stage>.neon] の typo も対象にするため)
        cls.check_env_backed_section_keys(data)
        data["stage"] = stage
        cls.resolve_vpc(data)
        result = cls.model_validate(data)
        result._emit_advisories()
        return result

    def _emit_advisories(self) -> None:
        """検証を通った後の advisory (raise しない助言) を 1 回だけ stderr に出す。

        pydantic の mode="after" validator は再検証のたびに走るため、
        重複させたくない助言はここ (from_toml から 1 回) で出す。
        """
        for cf in self.cloudfront.values():
            for route in cf.routes:
                message = route.double_prefix_advisory()
                if message:
                    echo.warning(message)

    @classmethod
    def resolve_vpc(cls, data: dict):
        """use_vpc に基づいて awscontainer.vpc と rds.vpc を解決"""
        vpc_data = data.get("vpc")

        for section in ("awscontainer", "rds"):
            if section not in data:
                continue
            use_vpc = data[section].pop("use_vpc", None)
            if use_vpc is None:  # auto
                if vpc_data:
                    data[section]["vpc"] = vpc_data
            elif use_vpc is True:
                if not vpc_data:
                    raise ValueError(
                        f"{section}.use_vpc=true ですが [vpc] が定義されていません"
                    )
                data[section]["vpc"] = vpc_data
            # use_vpc = False: VPC を使わない

    @classmethod
    def check_generator_version(cls, text: str):
        """runtime.toml の生成元 (CLI) 版と自身の runtime 版を突合する。

        pocket.runtime.toml は CLI (magic-pocket-cli) が生成し image に焼き込むが、
        Lambda 内 runtime (magic-pocket) はプロジェクトの uv.lock 依存で別に固定される。
        CLI が新機能スキーマを書き runtime が古いと pydantic が読めず INIT で opaque に
        落ちる (`Runtime.Unknown`)。生成元版 > 自身の版なら原因と対処が分かる例外に
        リフレーミングして早期に止める。マーカーは TOML コメントなので旧 runtime は無視
        (後方互換)。この検査を含む版以降の runtime でのみ効く。
        """
        from pocket import __version__ as runtime_version
        from pocket.utils import parse_generator_version, version_tuple

        generator = parse_generator_version(text)
        if not generator:
            return
        if version_tuple(generator) > version_tuple(runtime_version):
            raise ValueError(
                "pocket.runtime.toml は magic-pocket-cli %s が生成しましたが、この "
                "runtime (magic-pocket) は %s です。新しい pocket.toml 機能を古い "
                "runtime が解釈できず Lambda init が失敗します。上げてください: "
                "uv add 'magic-pocket[django]>=%s'"
                % (generator, runtime_version, generator)
            )

    @classmethod
    def check_keys(cls, data: dict):
        # stage は from_toml が注入するフィールドで、pocket.toml のキーではない
        valid_keys = [key for key in cls.model_fields if key != "stage"]
        valid_keys += data["general"]["stages"]
        for key in data:
            if key not in valid_keys:
                error = f"invalid key {key} in pocket.toml\n"
                error += "If it's a stage name, add it to stages."
                raise ValueError(error)

    @classmethod
    def check_env_backed_section_keys(cls, data: dict):
        """[neon] / [tidb] / [upstash] の toml キーの typo を検出する。

        他の設定クラスは model_config の extra="forbid" で typo を弾くが、この 3 つは
        credential を .env から読む BaseSettings 派生なので forbid にできない。
        forbid にすると .env の無関係なキー (DJANGO_SECRET_KEY 等) まで
        "Extra inputs are not permitted" で拒否されてしまう (nested validation でも
        dotenv source は読まれるため、Settings 経由でも同じ)。
        そこで model_config は extra="ignore" のまま、toml 側のキーだけをここで
        検証し、forbid 相当の typo 検出を得る。
        """
        for section, model in (("neon", Neon), ("tidb", TiDb), ("upstash", Upstash)):
            section_data = data.get(section)
            if not isinstance(section_data, dict):
                continue
            valid_keys = set()
            for name, field in model.model_fields.items():
                valid_keys.add(name)
                if field.alias:
                    valid_keys.add(field.alias)
            # 廃止済みキーは通し、各モデルの _reject_legacy_skip_check_existing に
            # 移行手順つきのエラーを出させる (ここで弾くと案内が失われる)
            valid_keys.add("skip_check_existing")
            for key in section_data:
                if key not in valid_keys:
                    raise ValueError(
                        f"invalid key {key} in [{section}] of pocket.toml\n"
                        f"  有効なキー: {sorted(valid_keys - {'skip_check_existing'})}"
                    )

    @classmethod
    def check_stage(cls, stage: str, data: dict):
        stages = data["general"]["stages"]
        if stage not in stages:
            raise ValueError(
                f"ステージ '{stage}' は pocket.toml に定義されていません。\n"
                f"  定義済みステージ: {stages}\n"
                f"  --stage オプションまたは POCKET_DEPLOY_STAGE 環境変数を"
                f"確認してください。"
            )

    @classmethod
    def merge_stage_data(cls, stage: str, data: dict):
        _deep_merge(data, data.get(stage, {}))

    @classmethod
    def remove_stages_data(cls, stage: str, data: dict):
        for s in data["general"]["stages"]:
            data.pop(s, None)
