"""stored user secret の SSM / Secrets Manager 名を導出する公開 API。

外部 provisioner (URL を焼く側) が「pocket がどのパスに backend の接続 URL を
保存/読み取りするか」を再実装せずに済むよう、pocket 側の正準導出を single source
of truth として公開する。値を put する側 (provisioner) と read する側 (deploy) が
同じ導出を共有でき、パス不一致による ParameterNotFound を構造的に防ぐ。

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
