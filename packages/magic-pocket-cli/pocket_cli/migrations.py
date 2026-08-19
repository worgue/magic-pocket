"""リリース跨ぎの移行処理 (deploy が毎回呼ぶ migrate フェーズ)。

旧配置のリソースを新配置へ引き継ぐ・掃除する類のロジックは、deploy 本体や
mediator の生成ループに埋め込まず、この registry に「どのリリースで導入した
移行か」を明示して集約する。deploy は毎回ここを呼び、各 migration は冪等で
移行済み環境では実質 no-op になる。

ライフサイクルの方針:

- **追加**: 破壊的変更を入れるリリースで Migration を registry に足す
- **維持**: 少なくとも数 minor の間は残す (deploy を 1 回でも挟めば移行が完了
  する状態を保つ)
- **削除**: registry から外すリリースの CHANGELOG に「<外す版> 以降へ上げる前に
  <移行を含む版> で一度 deploy する (または `pocket migrate` を実行する)」と
  明記する。これを飛ばした環境で何が起きるかも書く

実行タイミングは 2 相:

- ``run_deploy_ensure``: リソース生成の前 (旧配置からの値の引き継ぎ等)。
  mediator の secret 生成より先に走る必要があるものはここ
- ``run_deploy_cleanup``: deploy 成功後 (旧配置の削除。旧 stack の Lambda が
  CloudFront 切替まで旧配置を読み続けるため、成功後でないと消せないもの)

`pocket migrate` CLI からも同じ registry を呼べる (deploy を伴わない事前移行)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

import boto3

from pocket.utils import echo
from pocket_cli.cli import interaction

if TYPE_CHECKING:
    from pocket.context import Context


# --- 0.29.0: [awscontainer] → [container.<name>] --------------------------------


def _ensure_container_secret_inheritance(context: Context):
    """旧 project 共有パスの managed secret を container store へ引き継ぐ。

    0.29.0 で shared = true でない managed secret は container store
    ({stage}-{project}-{name}-{namespace}) へ移った。container store に無く
    旧 project パスにある key は、再生成せず値をコピーする (SECRET_KEY を
    再生成すると Django の session / 署名 cookie が無効化されるため)。
    shared 宣言の正規の住人 (shared store の現役 key) は移行残骸ではないため
    引き継ぎ元にしない — shared と無印の混在構成で、無印側が共有値を初期値と
    してコピーしてしまうのを防ぐ。
    """
    shared = context.secrets
    if shared is None:
        return
    legacy = shared.pocket_store.secrets
    if not legacy:
        return
    for c_name in sorted(context.container):
        sc = context.container[c_name].secrets
        if sc is None:
            continue
        stored = sc.pocket_store.secrets
        inherited = {
            key: legacy[key]
            for key in sc.managed
            if key not in stored and key in legacy and key not in shared.managed
        }
        if not inherited:
            continue
        for key in sorted(inherited):
            echo.log(
                "secret '%s' を旧 project 共有パスから container store (%s) へ"
                "引き継ぎます (値は再生成しません)。" % (key, sc.pocket_key)
            )
        sc.pocket_store.update_secrets(stored.copy() | inherited)


def _cleanup_legacy_secret_residue(context: Context):
    """shared store に残った旧配置の非共有 secret を確認付きで削除する。

    引き継ぎ (`_ensure_container_secret_inheritance`) 後も旧パス側は
    「旧 stack の Lambda が CloudFront 切替まで読み続ける」ため deploy 中は
    消せない。deploy 成功後のこのフックで、container store への引き継ぎが
    確認できたキーだけを旧パスから削除する。冪等 (残骸が無ければ何もしない)。
    """
    shared = context.secrets
    if shared is None:
        return
    shared_keys = set(shared.pocket_store.secrets.keys())
    # key → その key を宣言している container store view の一覧。同名の無印宣言は
    # 独立した値として複数 container に存在しうるため、旧パスの削除は
    # 「宣言している全 container がコピー済み」を条件にする (片方だけコピー済みの
    # 段階で消すと、もう片方が引き継ぎ元を失う)
    declaring: dict[str, list] = {}
    for c_name in sorted(context.container):
        sc = context.container[c_name].secrets
        if sc is None:
            continue
        for key in sc.managed:
            declaring.setdefault(key, []).append(sc)
    residue: set[str] = set()
    for key, views in declaring.items():
        if key not in shared_keys:
            continue
        if all(key in view.pocket_store.secrets for view in views):
            residue.add(key)
    # shared 宣言 / 自動注入のキーは正規の住人なので残す (防御的)
    residue -= set(shared.managed)
    if not residue:
        return
    echo.warning(
        "旧 project 共有パス (%s) に container store へ移行済みの secret が"
        "残っています: %s" % (shared.pocket_key, ", ".join(sorted(residue)))
    )
    if interaction.confirm("旧パス側の残骸を削除しますか？", default=True):
        shared.pocket_store.delete_secret_keys(residue)
        echo.success("旧パスの secret 残骸を削除しました。")


def _cleanup_legacy_container_resources(context: Context):
    """0.29.0 以前の単数 [awscontainer] 由来の旧リソースを検出して削除する。

    旧 container stack ({slug}-container) は scheduler / SQS event source を
    持ったまま残ると旧コードの cron / queue 消費が動き続けるため、放置は
    実害がある。新 stack + cloudfront 切替が完了した deploy の後 (= 旧 stack が
    参照フリーになった後) に、確認プロンプト付きで削除する (-y で自動承認)。
    旧 ECR repo ({prefix}lambda) も、どの container も ecr_name で参照して
    いなければ削除する。冪等 (無ければ何もしない)。
    """
    if not context.container or not context.general:
        return
    slug = f"{context.stage}-{context.project_name}"
    region = context.general.region
    legacy_stack_name = f"{slug}-container"
    cfn = boto3.client("cloudformation", region_name=region)
    try:
        cfn.describe_stacks(StackName=legacy_stack_name)
        stack_exists = True
    except cfn.exceptions.ClientError:
        stack_exists = False
    if stack_exists:
        echo.warning(
            "旧形式の container stack '%s' が残っています (0.29.0 の "
            "multi-container 化で stack 名が {slug}-container-{name} に"
            "変わりました)。旧 stack の scheduler / SQS が動き続けるため、"
            "削除を推奨します。" % legacy_stack_name
        )
        if interaction.confirm(
            "旧 stack '%s' を削除しますか？" % legacy_stack_name, default=True
        ):
            cfn.delete_stack(StackName=legacy_stack_name)
            echo.log("旧 stack の削除を開始しました (完了待ちはしません)。")
    resource_prefix = context.general.prefix_template.format(
        stage=context.stage,
        project=context.project_name,
        namespace=context.general.namespace,
    )
    legacy_repo = f"{resource_prefix}lambda"
    if any(c.ecr_name == legacy_repo for c in context.container.values()):
        return
    ecr = boto3.client("ecr", region_name=region)
    try:
        ecr.describe_repositories(repositoryNames=[legacy_repo])
    except ecr.exceptions.RepositoryNotFoundException:
        return
    echo.warning(
        "旧形式の ECR repository '%s' が残っています (新しい repo 名は "
        "{prefix}{container}-lambda)。" % legacy_repo
    )
    if interaction.confirm(
        "旧 ECR repository '%s' を削除しますか？" % legacy_repo, default=True
    ):
        ecr.delete_repository(repositoryName=legacy_repo, force=True)
        echo.success("旧 ECR repository を削除しました。")


# --- registry -------------------------------------------------------------------


@dataclass(frozen=True)
class Migration:
    """1 つのリリース跨ぎ移行。ensure / cleanup のどちらか (または両方) を持つ。"""

    version: str  # 導入リリース
    description: str
    ensure: Callable[[Context], None] | None = None
    cleanup: Callable[[Context], None] | None = None


MIGRATIONS: list[Migration] = [
    Migration(
        version="0.29.0",
        description=(
            "managed secret の container store 分離: 旧 project 共有パスの値を"
            "container store へ引き継ぎ、deploy 成功後に旧パスの残骸を削除する"
        ),
        ensure=_ensure_container_secret_inheritance,
        cleanup=_cleanup_legacy_secret_residue,
    ),
    Migration(
        version="0.29.0",
        description=(
            "旧 {slug}-container stack / {prefix}lambda ECR repo の削除"
            " (旧 stack の scheduler / SQS が動き続ける事故防止)"
        ),
        cleanup=_cleanup_legacy_container_resources,
    ),
]


def run_deploy_ensure(context: Context):
    """リソース生成前の移行 (旧配置からの引き継ぎ等) を全件実行する。"""
    for migration in MIGRATIONS:
        if migration.ensure is not None:
            migration.ensure(context)


def run_deploy_cleanup(context: Context):
    """deploy 成功後の移行掃除 (旧配置の削除) を全件実行する。"""
    for migration in MIGRATIONS:
        if migration.cleanup is not None:
            migration.cleanup(context)
