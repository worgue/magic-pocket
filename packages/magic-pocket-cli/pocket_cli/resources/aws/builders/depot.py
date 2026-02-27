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

    def _get_token(self) -> str:
        token = os.environ.get("DEPOT_TOKEN") or os.environ.get("DEPOT_API_KEY")
        if not token:
            raise RuntimeError(
                "DEPOT_TOKEN または DEPOT_API_KEY 環境変数が設定されていません"
            )
        return token

    def build_and_push(
        self,
        *,
        target: str,
        dockerfile_path: str,
        platform: str,
    ) -> None:
        token = self._get_token()

        print("Depot でイメージをビルドします...")
        print("  target: %s" % target)
        print("  dockerfile: %s" % dockerfile_path)
        print("  platform: %s" % platform)

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
        ]

        if self.project_id:
            cmd.extend(["--project", self.project_id])
            print("  project: %s" % self.project_id)

        env = {**os.environ, "DEPOT_TOKEN": token}
        subprocess.run(cmd, check=True, env=env)
        print("Depot ビルド完了")

    def delete(self) -> None:
        pass
