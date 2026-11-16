from __future__ import annotations

from pathlib import Path

from python_on_whales import docker

from pocket_cli.resources.aws.builders.context_check import (
    warn_files_without_world_read,
)


class DockerBuilder:
    def __init__(self, *, region: str) -> None:
        self.region = region

    def build_and_push(
        self,
        *,
        target: str,
        context_dir: Path,
        dockerfile_path: str,
        platform: str,
    ) -> None:
        # context は生の permission のまま image に入るため build 前に検査する
        # (codebuild backend は zip 時に正規化されるので不要)
        warn_files_without_world_read(context_dir)
        print("Building docker image...")
        print("  context: %s" % context_dir)
        print("  dockerpath: %s" % dockerfile_path)
        print("  tags: %s" % target)
        print("  platforms: %s" % platform)
        print("Logging in to ecr...")
        docker.login_ecr(region_name=self.region)
        print("Pushing docker image...")
        docker.build(
            str(context_dir),
            file=str(context_dir / dockerfile_path),
            tags=target,
            platforms=[platform],
            provenance=False,
            push=True,
        )

    def delete(self) -> None:
        pass
