"""pocket の内部命名 (stored user secret / ECR repo 等) を導出する公開 API。

外部ツールが pocket の命名規約を再実装せずに済むよう、pocket 側の正準導出を
single source of truth として公開する。

- stored user secret: 外部 provisioner (URL を焼く側) と deploy (読む側) が同じ
  導出を共有でき、パス不一致による ParameterNotFound を構造的に防ぐ
- ECR repo 名 / image タグ: pocket と併走する deploy 系が pocket のビルドした
  イメージを参照する際の repo 名 / タグ規約の drift を防ぐ

Python から:

    from pocket.naming import stored_user_secret_name, TIDB_DATABASE_URL

    name = stored_user_secret_name(
        project="pocket-example", stage="sandbox", secret_type=TIDB_DATABASE_URL
    )
    # -> "/sandbox-pocket-example-pocket-user/tidb_database_url"

このモジュールは pydantic / boto3 等の重い依存を持たない (純粋な文字列導出のみ) ので、
`import pocket` を軽量に保ったまま外部から利用できる。
"""

from __future__ import annotations

# stored user secret の type (= 保存 segment)。settings.UserSecretType と一致させる。
NEON_DATABASE_URL = "neon_database_url"
TIDB_DATABASE_URL = "tidb_database_url"  # noqa: S105 (type 名であって secret 値ではない)
UPSTASH_REDIS_URL = "upstash_redis_url"  # noqa: S105 (同上)

#: user_secret_path が受け付ける store。
STORE_SSM = "ssm"
STORE_SM = "sm"

DEFAULT_NAMESPACE = "pocket"
DEFAULT_POCKET_KEY_FORMAT = "{stage}-{project}-{namespace}"

#: [general].prefix_template の既定値 (general_settings.GeneralSettings と一致させる)。
DEFAULT_PREFIX_TEMPLATE = "{stage}-{project}-{namespace}-"

#: ECR repository 名の suffix (repo 名 = resource_prefix + この値)。
ECR_REPO_SUFFIX = "lambda"


def user_secret_path(pocket_key: str, segment: str, store: str) -> str:
    """stored user secret の正準名を導出する。

    provisioning identity を安定させるため、``segment`` には backend の type
    (``neon_database_url`` 等) を渡す。consumer の env var 名 (secrets.user の
    辞書キー) には依存させない — キーのリネームや backend 付け替えで保存先が
    動かないようにするため。managed の pocket_store パス ``/{pocket_key}/...``
    と衝突させないため ``{pocket_key}-user`` prefix 配下に置く
    (cleanup は該当パス配下のみ走査)。
    """
    prefix = f"{pocket_key}-user"
    if store == STORE_SSM:
        return f"/{prefix}/{segment}"
    return f"{prefix}/{segment}"


def pocket_key(
    *,
    project: str,
    stage: str,
    namespace: str = DEFAULT_NAMESPACE,
    pocket_key_format: str = DEFAULT_POCKET_KEY_FORMAT,
) -> str:
    """pocket_key を組み立てる (既定 ``{stage}-{project}-{namespace}``)。

    pocket.toml の ``[awscontainer.secrets].pocket_key_format`` を変えている場合のみ
    ``pocket_key_format`` を渡す。既定運用ならデフォルトのままでよい。
    """
    return pocket_key_format.format(stage=stage, project=project, namespace=namespace)


def stored_user_secret_name(
    *,
    project: str,
    stage: str,
    secret_type: str,
    store: str = STORE_SSM,
    namespace: str = DEFAULT_NAMESPACE,
    pocket_key_format: str = DEFAULT_POCKET_KEY_FORMAT,
) -> str:
    """stored user secret の正準名を ``(project, stage, secret_type)`` から導出する。

    例) ``project="pocket-example"``, ``stage="sandbox"``,
        ``secret_type=TIDB_DATABASE_URL`` →
        ``"/sandbox-pocket-example-pocket-user/tidb_database_url"``

    provisioner はこの名前へ接続 URL を put すれば、consumer の pocket.toml が
    ``DATABASE_URL = { type = "tidb_database_url" }`` と宣言するだけで deploy が読める
    (``name =`` での手動一致は不要)。
    """
    key = pocket_key(
        project=project,
        stage=stage,
        namespace=namespace,
        pocket_key_format=pocket_key_format,
    )
    return user_secret_path(key, secret_type, store)


def ecr_repo_name(
    *,
    project: str,
    stage: str,
    namespace: str = DEFAULT_NAMESPACE,
    prefix_template: str = DEFAULT_PREFIX_TEMPLATE,
    ecr_name: str | None = None,
) -> str:
    """pocket が build / deploy に使う ECR repository 名を導出する。

    既定は ``{stage}-{project}-{namespace}-lambda``
    (例: ``sandbox-myprj-pocket-lambda``)。外部ツール (pocket と併走する deploy 系)
    が pocket のビルドしたイメージを参照する際、内部命名の再実装による drift を
    防ぐための SoT。

    例) ``project="myprj"``, ``stage="sandbox"`` → ``"sandbox-myprj-pocket-lambda"``

    注意: この関数は pocket.toml を読まない純関数のため、toml で規約を上書きして
    いる構成ではその値を引数で渡すこと。渡さないと実際の repo 名と食い違う:

    - ``[awscontainer].ecr_name`` を明示している場合は ``ecr_name=`` に渡す
      (そのまま返る)
    - ``[general].prefix_template`` を変えている場合は ``prefix_template=`` に渡す

    pocket.toml を読んで常に正確な値 (deploy 済み実イメージの digest 付き URI) を
    得たい場合は CLI の ``pocket resource image uri --stage <stage>`` を使う。
    """
    if ecr_name:
        return ecr_name
    prefix = prefix_template.format(stage=stage, project=project, namespace=namespace)
    return prefix + ECR_REPO_SUFFIX


def ecr_image_tag(stage: str) -> str:
    """deploy 済みイメージの正準タグ (= stage) を返す。

    deploy は常に ``:{stage}`` タグの image を参照する。build once 運用では
    ``:{commit_hash}`` タグも併存するが、昇格 (promote) で ``:{stage}`` が
    その image に付け替えられるため、「今 deploy されている image」は常に
    ``:{stage}`` で引ける。
    """
    return stage
