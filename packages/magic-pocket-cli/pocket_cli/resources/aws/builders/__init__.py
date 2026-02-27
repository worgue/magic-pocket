from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pocket.context import BuildContext


class Builder(Protocol):
    def build_and_push(
        self,
        *,
        target: str,
        dockerfile_path: str,
        platform: str,
    ) -> None: ...

    def delete(self) -> None:
        """ビルドバックエンドのリソースを削除（不要なら何もしない）"""
        ...


def create_builder(
    build_context: BuildContext,
    *,
    region: str,
    resource_prefix: str,
    state_bucket: str,
    permissions_boundary: str | None = None,
) -> Builder:
    backend = build_context.backend

    if backend == "docker":
        from pocket_cli.resources.aws.builders.docker import DockerBuilder

        return DockerBuilder(region=region)

    if backend == "codebuild":
        from pocket_cli.resources.aws.builders.codebuild import CodeBuildBuilder

        return CodeBuildBuilder(
            region=region,
            resource_prefix=resource_prefix,
            state_bucket=state_bucket,
            compute_type=build_context.compute_type,
            permissions_boundary=permissions_boundary,
        )

    if backend == "depot":
        from pocket_cli.resources.aws.builders.depot import DepotBuilder

        return DepotBuilder(
            region=region,
            project_id=build_context.depot_project_id,
        )

    raise ValueError(f"不明なビルドバックエンド: {backend}")
