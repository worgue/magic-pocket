from __future__ import annotations

import os
import subprocess


class DepotBuilder:
    def __init__(
        self,
        *,
        region: str,
        project_id: str | None = None,
    ) -> None:
        self.region = region
        self.project_id = project_id or os.environ.get("DEPOT_PROJECT_ID")

    def build_and_push(
        self,
        *,
        target: str,
        dockerfile_path: str,
        platform: str,
    ) -> None:
        token = os.environ.get("DEPOT_TOKEN")
        if not token:
            raise RuntimeError("DEPOT_TOKEN 環境変数が設定されていません")
        if not self.project_id:
            raise RuntimeError(
                "Depot project ID が未設定です。"
                "pocket.toml の [awscontainer.build] depot_project_id か "
                "DEPOT_PROJECT_ID 環境変数を設定してください"
            )

        print("Depot でイメージをビルドします...")
        print("  target: %s" % target)
        print("  dockerfile: %s" % dockerfile_path)
        print("  platform: %s" % platform)
        print("  project: %s" % self.project_id)

        cmd = [
            "depot",
            "build",
            ".",
            "--file",
            dockerfile_path,
            "--tag",
            target,
            "--platform",
            platform,
            "--push",
            "--project",
            self.project_id,
        ]

        subprocess.run(cmd, check=True)
        print("Depot ビルド完了")

    def delete(self) -> None:
        pass
