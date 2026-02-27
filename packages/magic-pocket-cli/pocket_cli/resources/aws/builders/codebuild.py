from __future__ import annotations

import io
import json
import os
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from pocket_cli.resources.aws.builders.dockerignore import (
    parse_dockerignore,
    should_include,
)

# CodeBuildイメージ
IMAGE_AMD64 = "aws/codebuild/amazonlinux-x86_64-standard:5.0"
IMAGE_ARM64 = "aws/codebuild/amazonlinux-aarch64-standard:3.0"

BUILDSPEC = (
    "version: 0.2\n"
    "phases:\n"
    "  pre_build:\n"
    "    commands:\n"
    "      - >-\n"
    "        aws ecr get-login-password --region $AWS_DEFAULT_REGION\n"
    "        | docker login --username AWS\n"
    "        --password-stdin $ECR_HOST\n"
    "  build:\n"
    "    commands:\n"
    "      - docker build -t $IMAGE_TAG -f $DOCKERFILE .\n"
    "  post_build:\n"
    "    commands:\n"
    "      - docker push $IMAGE_TAG\n"
)

POLL_INTERVAL = 10


class CodeBuildBuilder:
    def __init__(
        self,
        *,
        region: str,
        resource_prefix: str,
        state_bucket: str,
        compute_type: str = "BUILD_GENERAL1_MEDIUM",
        permissions_boundary: str | None = None,
    ) -> None:
        self.region = region
        self.resource_prefix = resource_prefix
        self.state_bucket = state_bucket
        self.compute_type = compute_type
        self.permissions_boundary = (
            os.environ.get("CODEBUILD_PERMISSIONS_BOUNDARY") or permissions_boundary
        )

        self.codebuild = boto3.client("codebuild", region_name=region)
        self.iam = boto3.client("iam", region_name=region)
        self.s3 = boto3.client("s3", region_name=region)
        self.sts = boto3.client("sts", region_name=region)

        self._project_name = f"{resource_prefix}codebuild"
        self._role_name = f"forge-{resource_prefix}codebuild-role"
        self._source_key = f"codebuild/{self._project_name}/source.zip"

    def build_and_push(
        self,
        *,
        target: str,
        dockerfile_path: str,
        platform: str,
    ) -> None:
        print("CodeBuild でイメージをビルドします...")
        print("  target: %s" % target)
        print("  dockerfile: %s" % dockerfile_path)
        print("  platform: %s" % platform)

        account_id = self.sts.get_caller_identity()["Account"]
        ecr_host = f"{account_id}.dkr.ecr.{self.region}.amazonaws.com"

        role_arn = self._ensure_role(account_id)
        self._ensure_project(platform, role_arn)
        self._upload_source(dockerfile_path)

        build_id = self._start_build(
            target=target,
            dockerfile_path=dockerfile_path,
            ecr_host=ecr_host,
        )
        self._wait_build(build_id)

        # ソースzip削除
        self.s3.delete_object(Bucket=self.state_bucket, Key=self._source_key)
        print("CodeBuild ビルド完了")

    def delete(self) -> None:
        """CodeBuildプロジェクトとIAMロールを削除"""
        self._delete_project()
        self._delete_role()

    # --- IAM ロール ---

    def _ensure_role(self, account_id: str) -> str:
        try:
            resp = self.iam.get_role(RoleName=self._role_name)
            return resp["Role"]["Arn"]  # type: ignore[return-value]
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchEntity":
                raise

        print("  CodeBuild用IAMロールを作成: %s" % self._role_name)
        assume_role_policy = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "codebuild.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            }
        )

        create_kwargs: dict = {
            "RoleName": self._role_name,
            "AssumeRolePolicyDocument": assume_role_policy,
        }
        if self.permissions_boundary:
            create_kwargs["PermissionsBoundary"] = self.permissions_boundary

        resp = self.iam.create_role(**create_kwargs)
        role_arn: str = resp["Role"]["Arn"]

        policy = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "ecr:GetAuthorizationToken",
                        ],
                        "Resource": "*",
                    },
                    {
                        "Effect": "Allow",
                        "Action": [
                            "ecr:BatchCheckLayerAvailability",
                            "ecr:GetDownloadUrlForLayer",
                            "ecr:BatchGetImage",
                            "ecr:PutImage",
                            "ecr:InitiateLayerUpload",
                            "ecr:UploadLayerPart",
                            "ecr:CompleteLayerUpload",
                        ],
                        "Resource": (
                            f"arn:aws:ecr:{self.region}:{account_id}:repository/*"
                        ),
                    },
                    {
                        "Effect": "Allow",
                        "Action": [
                            "s3:GetObject",
                            "s3:GetObjectVersion",
                        ],
                        "Resource": (f"arn:aws:s3:::{self.state_bucket}/codebuild/*"),
                    },
                    {
                        "Effect": "Allow",
                        "Action": [
                            "logs:CreateLogGroup",
                            "logs:CreateLogStream",
                            "logs:PutLogEvents",
                        ],
                        "Resource": (
                            f"arn:aws:logs:{self.region}"
                            f":{account_id}:log-group:"
                            f"/aws/codebuild/"
                            f"{self._project_name}*"
                        ),
                    },
                ],
            }
        )
        self.iam.put_role_policy(
            RoleName=self._role_name,
            PolicyName="codebuild-policy",
            PolicyDocument=policy,
        )

        # IAMロールの伝播待ち
        print("  IAMロール伝播を待機中...")
        time.sleep(10)
        return role_arn

    def _delete_role(self) -> None:
        try:
            # インラインポリシー削除
            policies = self.iam.list_role_policies(RoleName=self._role_name)
            for policy_name in policies["PolicyNames"]:
                self.iam.delete_role_policy(
                    RoleName=self._role_name, PolicyName=policy_name
                )
            self.iam.delete_role(RoleName=self._role_name)
            print("  IAMロール削除: %s" % self._role_name)
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchEntity":
                return
            raise

    # --- CodeBuild プロジェクト ---

    def _ensure_project(self, platform: str, role_arn: str) -> None:
        env_type, image = self._env_for_platform(platform)

        project_config = {
            "name": self._project_name,
            "source": {
                "type": "S3",
                "location": f"{self.state_bucket}/{self._source_key}",
            },
            "artifacts": {"type": "NO_ARTIFACTS"},
            "environment": {
                "type": env_type,
                "image": image,
                "computeType": self.compute_type,
                "privilegedMode": True,
            },
            "serviceRole": role_arn,
        }

        try:
            self.codebuild.batch_get_projects(names=[self._project_name])
            existing = self.codebuild.batch_get_projects(names=[self._project_name])
            if existing["projects"]:
                self.codebuild.update_project(**project_config)
                print("  CodeBuildプロジェクト更新: %s" % self._project_name)
                return
        except ClientError:
            pass

        self.codebuild.create_project(**project_config)
        print("  CodeBuildプロジェクト作成: %s" % self._project_name)

    def _delete_project(self) -> None:
        try:
            self.codebuild.delete_project(name=self._project_name)
            print("  CodeBuildプロジェクト削除: %s" % self._project_name)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                return
            raise

    @staticmethod
    def _env_for_platform(platform: str) -> tuple[str, str]:
        if "arm64" in platform or "aarch64" in platform:
            return "ARM_CONTAINER", IMAGE_ARM64
        return "LINUX_CONTAINER", IMAGE_AMD64

    # --- ソースアップロード ---

    def _upload_source(self, dockerfile_path: str) -> None:
        print("  ソースをS3にアップロード中...")
        # Dockerfileの親ディレクトリを基準にコンテキスト決定
        context_dir = Path(".").resolve()
        patterns = parse_dockerignore(context_dir)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(context_dir.rglob("*")):
                if path.is_dir():
                    continue
                rel = str(path.relative_to(context_dir))
                if not should_include(rel, patterns):
                    continue
                zf.write(path, rel)

        buf.seek(0)
        self.s3.put_object(
            Bucket=self.state_bucket,
            Key=self._source_key,
            Body=buf.read(),
        )
        zip_size_mb = buf.tell() / (1024 * 1024)
        print("  アップロード完了 (%.1f MB)" % zip_size_mb)

    # --- ビルド実行 ---

    def _start_build(
        self,
        *,
        target: str,
        dockerfile_path: str,
        ecr_host: str,
    ) -> str:
        resp = self.codebuild.start_build(
            projectName=self._project_name,
            buildspecOverride=BUILDSPEC,
            environmentVariablesOverride=[
                {"name": "IMAGE_TAG", "value": target, "type": "PLAINTEXT"},
                {"name": "DOCKERFILE", "value": dockerfile_path, "type": "PLAINTEXT"},
                {"name": "ECR_HOST", "value": ecr_host, "type": "PLAINTEXT"},
            ],
        )
        build_id: str = resp["build"]["id"]
        print("  ビルド開始: %s" % build_id)
        return build_id

    def _wait_build(self, build_id: str) -> None:
        print("  ビルド完了を待機中...")
        while True:
            resp = self.codebuild.batch_get_builds(ids=[build_id])
            build = resp["builds"][0]
            status = build["buildStatus"]

            if status == "SUCCEEDED":
                return
            if status in ("FAILED", "FAULT", "STOPPED", "TIMED_OUT"):
                phases = build.get("phases", [])
                _print_failed_phases(phases)
                raise RuntimeError(
                    "CodeBuild ビルド失敗: %s (status=%s)" % (build_id, status)
                )

            time.sleep(POLL_INTERVAL)

    # --- リソース存在チェック ---

    def project_exists(self) -> bool:
        resp = self.codebuild.batch_get_projects(names=[self._project_name])
        return len(resp.get("projects", [])) > 0

    def role_exists(self) -> bool:
        try:
            self.iam.get_role(RoleName=self._role_name)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchEntity":
                return False
            raise


def _print_failed_phases(phases: list[dict]) -> None:
    print("  --- CodeBuild フェーズ情報 ---")
    for phase in phases:
        phase_type = phase.get("phaseType", "?")
        phase_status = phase.get("phaseStatus", "?")
        if phase_status not in ("SUCCEEDED", "?"):
            contexts = phase.get("contexts", [])
            msg = contexts[0].get("message", "") if contexts else ""
            print("  %s: %s %s" % (phase_type, phase_status, msg))
