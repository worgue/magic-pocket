from __future__ import annotations

import os
import shutil
import stat
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from pocket.utils import echo
from pocket_cli.resources.aws import iam_roles
from pocket_cli.resources.aws.builders.dockerignore import (
    iter_source_files,
    load_dockerignore,
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
        self._role_name = f"{resource_prefix}codebuild-role"
        # 0.36 以前のロール名。forge- は開発環境由来の prefix で pocket の命名規約
        # ({stage}-{project}-...) と無関係だったため改名した。ロールは stage の
        # 所有物なので、新ロールへ付け替えた後に pocket が削除する (改名互換。
        # いずれ削除予定)
        self._legacy_role_name = f"forge-{resource_prefix}codebuild-role"
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
        # project の serviceRole を新ロールへ付け替えた後なので、旧名ロールは孤児
        self._delete_role(self._legacy_role_name)
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
        self._delete_role(self._role_name)
        self._delete_role(self._legacy_role_name)

    # --- IAM ロール ---

    def _ensure_role(self, account_id: str) -> str:
        return iam_roles.ensure_role(
            self.iam,
            iam_roles.codebuild_role(
                name=self._role_name,
                region=self.region,
                account_id=account_id,
                state_bucket=self.state_bucket,
                project_name=self._project_name,
                permissions_boundary=self.permissions_boundary,
            ),
        )

    def _delete_role(self, role_name: str) -> None:
        iam_roles.delete_role(self.iam, role_name)

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

        # batch_get_projects は存在しない名前でも例外を投げず projectsNotFound に
        # 入るだけなので、1 回の呼び出しで存在判定できる
        existing = self.codebuild.batch_get_projects(names=[self._project_name])
        if existing["projects"]:
            self.codebuild.update_project(**project_config)
            print("  CodeBuildプロジェクト更新: %s" % self._project_name)
            return

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
        import tempfile

        print("  ソースをS3にアップロード中...")
        context_dir = Path(".").resolve()
        spec = load_dockerignore(context_dir)

        # SpooledTemporaryFile: 50MB までメモリ、超えたらディスクへ自動退避
        buf = tempfile.SpooledTemporaryFile(max_size=50 * 1024 * 1024)
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for abs_path, rel in iter_source_files(context_dir, spec):
                self._add_source_file(zf, abs_path, rel)

        zip_size_mb = buf.tell() / (1024 * 1024)
        buf.seek(0)
        # upload_fileobj でストリーミングアップロード
        self.s3.upload_fileobj(buf, self.state_bucket, self._source_key)
        buf.close()
        print("  アップロード完了 (%.1f MB)" % zip_size_mb)

    def _add_source_file(
        self, zf: zipfile.ZipFile, full_path: str, arcname: str
    ) -> None:
        """1 ファイルを zip に追加する（forge VM 由来の footgun を吸収）。

        - 壊れた symlink（host 側参照など、実体が無い）は skip して warning。
          そのまま ``zipfile.write`` すると ``FileNotFoundError`` で source zip
          作成ごと落ち、deploy 全体が失敗するため。
        - 通常ファイルの権限を 0644 / 実行ファイルを 0755 に正規化する。
          forge VM では ``sed -i`` 編集等で 0600 のファイルが生まれやすく、
          そのまま image に入ると Lambda の非 root 実行ユーザーが読めず起動時に
          panic する。mode を落として（他ユーザー read を保証して）防ぐ。
        """
        if not os.path.exists(full_path):
            # os.path.exists は symlink を辿るため、壊れた symlink で False
            echo.warning("  壊れた symlink を skip: %s" % arcname)
            return
        zi = zipfile.ZipInfo.from_file(full_path, arcname)
        zi.compress_type = zipfile.ZIP_DEFLATED
        perm = (zi.external_attr >> 16) & 0o777
        new_perm = 0o755 if perm & stat.S_IXUSR else 0o644
        zi.external_attr = (new_perm << 16) | (zi.external_attr & 0xFFFF)
        with open(full_path, "rb") as src, zf.open(zi, "w") as dst:
            shutil.copyfileobj(src, dst)

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
        """現行名・旧名いずれかのロールがあるか (destroy の対象判定用)。"""
        return any(
            self._role_exists(name)
            for name in (self._role_name, self._legacy_role_name)
        )

    def _role_exists(self, role_name: str) -> bool:
        try:
            self.iam.get_role(RoleName=role_name)
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
