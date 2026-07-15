from __future__ import annotations

import json
import mimetypes
import subprocess
import time
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel

from pocket.resources.base import ResourceStatus
from pocket.utils import echo
from pocket_cli.resources.aws.cloudformation import CloudFrontStack
from pocket_cli.resources.aws.s3_utils import delete_bucket_with_contents

if TYPE_CHECKING:
    from pocket.context import CloudFrontContext, RouteContext
    from pocket_cli.mediator import Mediator


class OriginAccessControl(BaseModel):
    Id: str
    Description: str | None = None
    Name: str
    SigningProtocol: Literal["sigv4"]
    SigningBehavior: Literal["never", "always", "no-override"]
    OriginAccessControlOriginType: Literal[
        "s3", "mediastore", "mediapackagev2", "lambda"
    ]


class NoOacException(Exception):
    pass


class CloudFront:
    context: CloudFrontContext

    def __init__(self, context: CloudFrontContext) -> None:
        self.context = context
        self.s3_client = boto3.client("s3", region_name=context.s3_region)
        self.cf_client = boto3.client("cloudfront")
        self._token_secret_value: str = ""
        self._origin_verify_secret_value: str = ""

    @property
    def description(self):
        return (
            "Create cloudformation(for cloudfront) using s3 bucket: %s"
            % self.context.bucket_name
        )

    def state_info(self):
        key = "cloudfront-%s" % self.context.name
        return {key: {"bucket_name": self.context.bucket_name}}

    def deploy_init(self):
        self.warn_contents()

    @property
    def status(self) -> ResourceStatus:
        return self.stack.status

    @property
    def stack(self):
        return CloudFrontStack(
            self.context,
            token_secret_value=self._token_secret_value,
            origin_verify_secret_value=self._origin_verify_secret_value,
        )

    def prepare_deploy(self, mediator: Mediator | None = None):
        """template hash に影響する secret 値を store から読み込む (副作用なし)。

        status / yaml_synced の判定前に呼ぶこと。値が空のまま hash を計算すると
        deploy 済み hash と一致せず、毎回 REQUIRE_UPDATE になる。
        """
        self._prepare_token_secret(mediator)
        self._prepare_origin_verify_secret(mediator)

    def create(self, mediator: Mediator | None = None):
        self.update(mediator=mediator)

    def update(self, mediator: Mediator | None = None):
        self.prepare_deploy(mediator)
        if not self.stack.exists:
            self.stack.create()
        elif not self.stack.yaml_synced:
            self.stack.update()
        info = echo.info
        log = echo.log
        log("Waiting for cloudformation stack to be completed ...")
        log("This may take a few minutes.")
        log("Because cloudfront distribution id is required to set s3 bucket policy.")
        info("If you want to exit, you can safely kill this process.")
        info("In that case, run `pocket resource cloudfront update` later.")
        self.stack.wait_status("COMPLETED", timeout=1800, interval=10)
        self._cleanup_legacy_redirect_from()
        self._ensure_bucket_policy()
        self._write_token_secret_to_kvs()
        log("Bucket for cloudfront is ready.")
        self.warn_contents()

    def ensure_post_deploy_state(self, mediator: Mediator | None = None):
        """stack 完了後に必要な後付け状態 (bucket policy / KVS) を冪等に確保する。

        update() の wait_status が timeout した場合、stack 自体はその後 COMPLETED
        になっても次回 deploy で status==COMPLETED と判定され update() が呼ばれず
        KVS 書き込みなどが永遠にスキップされる、という事故が起きる。これを防ぐため
        deploy フローの末尾で stack 状態によらず冪等に再実行する。
        """
        self.prepare_deploy(mediator)
        if self.stack.status != "COMPLETED":
            return
        self._ensure_bucket_policy()
        self._write_token_secret_to_kvs()

    def _prepare_token_secret(self, mediator: Mediator | None):
        if not self.context.token_secret:
            return
        if not mediator:
            return
        ac = mediator.context.awscontainer
        if not ac or not ac.secrets:
            return
        pocket_store = ac.secrets.pocket_store
        secrets = pocket_store.secrets
        if self.context.token_secret in secrets:
            value = secrets[self.context.token_secret]
            if isinstance(value, str):
                self._token_secret_value = value
            else:
                echo.warning(
                    "token_secret の値が文字列ではありません: %s"
                    % self.context.token_secret
                )
        else:
            echo.warning(
                "token_secret '%s' が managed secrets に見つかりません"
                % self.context.token_secret
            )

    def _prepare_origin_verify_secret(self, mediator: Mediator | None):
        """enable_origin_verify 時に origin verify secret の値を store から読む。

        値は CFn テンプレートの OriginCustomHeaders に焼き込まれる
        (token_secret の post-deploy KVS とは異なり、distribution config の一部
        なので create/update 時点で必要)。managed secret なので AwsContainer
        deploy 時に既に生成済み (get_resources は AwsContainer → CloudFront 順)。
        """
        from pocket.context import ORIGIN_VERIFY_SECRET_KEY

        if not self.context.enable_origin_verify:
            return
        if not mediator:
            return
        ac = mediator.context.awscontainer
        if not ac or not ac.secrets:
            return
        secrets = ac.secrets.pocket_store.secrets
        value = secrets.get(ORIGIN_VERIFY_SECRET_KEY)
        if isinstance(value, str):
            self._origin_verify_secret_value = value
        else:
            echo.warning(
                "origin verify secret '%s' が managed secrets に見つかりません"
                % ORIGIN_VERIFY_SECRET_KEY
            )

    def _write_token_secret_to_kvs(self):
        if not self._token_secret_value:
            return
        if not self.stack.output:
            echo.warning("スタック出力が取得できません。KVS 書き込みをスキップします。")
            return
        kvs_arn = self.stack.output.get("TokenKvsArn")
        if not kvs_arn:
            echo.warning("TokenKvsArn が出力に見つかりません。")
            return
        kvs_client = boto3.client(
            "cloudfront-keyvaluestore", region_name=self.context.region
        )
        desc = kvs_client.describe_key_value_store(KvsARN=kvs_arn)
        etag = desc["ETag"]
        kvs_client.put_key(
            KvsARN=kvs_arn,
            Key="token_secret",
            Value=self._token_secret_value,
            IfMatch=etag,
        )
        echo.info("KVS にトークンシークレットを書き込みました")

    def upload_managed_assets(self):
        """managed_assets のファイルを S3 に同期する (全件 upload + 不要削除)。"""
        if not self.context.managed_assets:
            return
        base = Path(self.context.managed_assets)
        stage_dir = base / self.context.stage
        if stage_dir.is_dir():
            asset_dir = stage_dir
        else:
            asset_dir = base / "default"
        if not asset_dir.is_dir():
            echo.warning("managed_assets ディレクトリが見つかりません: %s" % asset_dir)
            return
        bucket = self.context.bucket_name
        uploaded_keys: set[str] = set()
        for file in asset_dir.iterdir():
            if not file.is_file():
                continue
            s3_key = "pocket_managed/%s" % file.name
            uploaded_keys.add(s3_key)
            content_type = (
                mimetypes.guess_type(str(file))[0] or "application/octet-stream"
            )
            self.s3_client.upload_file(
                str(file),
                bucket,
                s3_key,
                ExtraArgs={"ContentType": content_type},
            )
            echo.log("managed_assets: s3://%s/%s" % (bucket, s3_key))
        paginator = self.s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix="pocket_managed/"):
            for obj in page.get("Contents", []):
                if obj["Key"] not in uploaded_keys:
                    self.s3_client.delete_object(Bucket=bucket, Key=obj["Key"])
                    echo.log("managed_assets 削除: s3://%s/%s" % (bucket, obj["Key"]))
        echo.info(
            "managed_assets: %d ファイルをアップロードしました" % len(uploaded_keys)
        )

    def upload(self, *, skip_build: bool = False):
        for route in self.context.uploadable_routes:
            if route.build and not skip_build:
                echo.info("ビルド実行: %s" % route.build)
                try:
                    # route.build は pocket.toml の build コマンド (設定者 =
                    # デプロイ実行者)。意図的な shell 実行のため S602 / semgrep を抑制。
                    subprocess.run(route.build, shell=True, check=True)  # noqa: S602  # nosemgrep
                except subprocess.CalledProcessError as e:
                    echo.danger(
                        "build コマンドが失敗しました (exit %d): %s"
                        % (e.returncode, route.build)
                    )
                    echo.warning(
                        "deploy ホスト側で依存を入れ直してから再実行してください。"
                        " 例えば npm/bun の optional dependency (rolldown 等) "
                        "が materialize されていないと、ロックファイルにあっても"
                        " import 時に Cannot find module で失敗します"
                        " (`rm -rf node_modules && npm ci` 等で復旧)。"
                    )
                    raise
            self._upload_route(route)
        if self.context.uploadable_routes:
            self._invalidate()

    def _upload_route(self, route: RouteContext):
        s3_prefix = (route.origin_path + route.path_pattern.rstrip("/*")).lstrip("/")
        if not route.build_dir:
            raise RuntimeError("route.build_dir is not set")
        local_dir = Path(route.build_dir)
        uploaded_keys: set[str] = set()
        for file in local_dir.rglob("*"):
            if file.is_dir():
                continue
            relative = file.relative_to(local_dir)
            s3_key = s3_prefix + "/" + str(relative)
            uploaded_keys.add(s3_key)
            extra_args: dict[str, str] = {
                "ContentType": mimetypes.guess_type(str(file))[0]
                or "application/octet-stream"
            }
            if route.is_spa:
                if file.suffix in (".html", ".htm"):
                    extra_args["CacheControl"] = "no-cache, no-store"
                else:
                    extra_args["CacheControl"] = "max-age=31536000"
            self.s3_client.upload_file(
                str(file),
                self.context.bucket_name,
                s3_key,
                ExtraArgs=extra_args,
            )
            echo.log("アップロード: s3://%s/%s" % (self.context.bucket_name, s3_key))
        self._delete_stale_objects(s3_prefix, uploaded_keys)
        echo.info(
            "%d ファイルをアップロードしました (prefix: %s)"
            % (len(uploaded_keys), s3_prefix)
        )

    def _delete_stale_objects(self, prefix: str, uploaded_keys: set[str]):
        paginator = self.s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=self.context.bucket_name, Prefix=prefix + "/"
        ):
            for obj in page.get("Contents", []):
                if obj["Key"] not in uploaded_keys:
                    self.s3_client.delete_object(
                        Bucket=self.context.bucket_name, Key=obj["Key"]
                    )
                    echo.log(
                        "削除: s3://%s/%s" % (self.context.bucket_name, obj["Key"])
                    )

    def _invalidate(self):
        self.cf_client.create_invalidation(
            DistributionId=self.distribution_id,
            InvalidationBatch={
                "Paths": {"Quantity": 1, "Items": ["/*"]},
                "CallerReference": str(int(time.time())),
            },
        )
        echo.info("CloudFront キャッシュ無効化をリクエストしました")

    def warn_contents(self):
        bucket = self.context.bucket_name
        for route in self.context.routes:
            if route.build_dir:
                continue
            origin = route.origin_path + route.path_pattern
            echo.warning("Upload files manually to s3://%s%s" % (bucket, origin))
            if route.is_spa:
                echo.info("%s is a spa route." % (route.path_pattern or "default"))
                echo.info("Set proper cahce headers.")
                eg_cmd = "npx s3-spa-upload build %s --delete --prefix %s" % (
                    bucket,
                    origin[1:],
                )
                echo.info("e.g) " + eg_cmd)
            elif route.versioning:
                echo.info("This is a versioned route.")
                echo.info(
                    "Just upload your files. CloudFront will set cache-control headers."
                )
                eg_cmd = "aws s3 sync data s3://%s%s" % (bucket, origin)
                echo.info("e.g) " + eg_cmd)

    def delete(self):
        self._cleanup_legacy_redirect_from()
        self._delete_bucket_policy()
        echo.info("Deleting cloudformation stack for cloudfront ...")
        self.stack.delete()
        self.stack.wait_status("NOEXIST", timeout=900, interval=15)
        echo.warning(
            "S3 bucket is managed by the S3 resource: " + self.context.bucket_name
        )

    def _cleanup_legacy_redirect_from(self):
        """旧 redirect_from 実装 (専用 S3 website バケット) の名残を撤去する。

        CloudFront Function 方式へ移行後、専用 distribution / cert は CFn 更新で
        撤去されるが、旧実装が命令的に作っていた S3 website バケット
        (バケット名 = redirect ドメイン) は CFn 管理外で orphan 化する。ここで
        冪等に削除する。別アカウント所有 / リージョン不一致等で削除できない場合は
        警告のみとし、deploy は止めない (新方式の 301 はバケットに依存しない)。
        """
        for redirect_from in self.context.redirect_from:
            bucket = redirect_from.domain
            try:
                delete_bucket_with_contents(self.s3_client, bucket)
            except ClientError:
                echo.warning(
                    "Legacy redirect-from bucket を削除できませんでした "
                    "(別アカウント所有 / リージョン不一致の可能性)。"
                    "不要なら手動で確認・削除してください: " + bucket
                )

    def _delete_redirect_from_policies(self, bucket_name):
        echo.danger("Delete redirect from bucket policies is implementing ...")
        echo.warning("Please delete the bucket policy manually.")
        echo.info("The bucket name: " + bucket_name)

    def _update_origin_bucket_policy(self, policy: dict | None):
        if policy is None:
            echo.info("Deleting bucket policy for %s." % self.context.bucket_name)
        else:
            echo.info("Updating bucket policy for %s." % self.context.bucket_name)
        echo.log("Current policy: %s" % self.bucket_policy)
        if policy is None:
            self.s3_client.delete_bucket_policy(Bucket=self.context.bucket_name)
        else:
            self.s3_client.put_bucket_policy(
                Bucket=self.context.bucket_name,
                Policy=json.dumps(policy),
            )
        echo.log("Updated policy: %s" % policy)
        del self.bucket_policy

    def _ensure_bucket_policy(self):
        if self.bucket_policy is None:
            self._update_origin_bucket_policy(
                {
                    "Version": self.bucket_policy_version_should_be,
                    "Statement": [self.bucket_policy_statement_should_contain],
                }
            )
        elif self.bucket_policy["Version"] != self.bucket_policy_version_should_be:
            echo.warning(
                "Bucket policy version %s will be upgraded to %s."
                % (self.bucket_policy["Version"], self.bucket_policy_version_should_be)
            )
            bucket_policy_should_be = self.bucket_policy.copy()
            bucket_policy_should_be["Version"] = self.bucket_policy_version_should_be
            if (
                self.bucket_policy_statement_should_contain
                not in bucket_policy_should_be["Statement"]
            ):
                bucket_policy_should_be["Statement"].append(
                    self.bucket_policy_statement_should_contain
                )
            self._update_origin_bucket_policy(bucket_policy_should_be)
        elif self.bucket_policy_require_update:
            bucket_policy_should_be = self.bucket_policy.copy()
            bucket_policy_should_be["Statement"].append(
                self.bucket_policy_statement_should_contain
            )
            self._update_origin_bucket_policy(bucket_policy_should_be)
        else:
            echo.info("Bucket policy is already configured properly.")

    def _delete_bucket_policy(self):
        delete_target = self.bucket_policy_statement_should_contain
        if self.bucket_policy is None:
            echo.info("Bucket policy is already None.")
        elif self.bucket_policy["Version"] != self.bucket_policy_version_should_be:
            echo.warning("Bucket policy version missmatch. Check the policy manually.")
        elif delete_target not in self.bucket_policy["Statement"]:
            echo.warning("No policy found. Check the policy manually.")
        else:
            bucket_policy_should_be = self.bucket_policy.copy()
            bucket_policy_should_be["Statement"].remove(delete_target)
            if not bucket_policy_should_be["Statement"]:
                self._update_origin_bucket_policy(None)
            else:
                self._update_origin_bucket_policy(bucket_policy_should_be)

    @property
    def bucket_policy_require_update(self):
        return (self.bucket_policy is None) or (
            self.bucket_policy_statement_should_contain
            not in self.bucket_policy["Statement"]
        )

    @cached_property
    def bucket_policy(self):
        try:
            return json.loads(
                self.s3_client.get_bucket_policy(Bucket=self.context.bucket_name)[
                    "Policy"
                ]
            )
        except ClientError:
            return None

    @cached_property
    def account_id(self):
        return boto3.client("sts").get_caller_identity().get("Account")

    @cached_property
    def distribution_id(self):
        if not self.stack.output:
            raise Exception("Cloudfront distribution is not created yet.")
        return self.stack.output["DistributionId"]

    @property
    def bucket_policy_version_should_be(self):
        return "2012-10-17"

    @property
    def bucket_policy_statement_should_contain(self):
        return {
            "Sid": "AllowCloudFrontReadOnly%s" % self.context.yaml_key,
            "Effect": "Allow",
            "Principal": {"Service": "cloudfront.amazonaws.com"},
            "Action": "s3:GetObject",
            "Resource": "arn:aws:s3:::%s%s/*"
            % (self.context.bucket_name, self.context.bucket_policy_prefix),
            "Condition": {
                "StringEquals": {
                    "AWS:SourceArn": "arn:aws:cloudfront::%s:distribution/%s"
                    % (self.account_id, self.distribution_id)
                }
            },
        }
