"""SecretsManager / SSM Parameter Store への単一値 I/O 共通ヘルパー。

`Rds._write/_read/_delete_credential_from_store` と
`StoredUserSecretStore` (stored user secret の per-name CRUD) に重複していた
「create or put / get / delete (+NotFound 握り)」を集約する。
CLI (pocket_cli) と runtime (pocket) の両方から使うため pocket 側に置く。

クライアントは各関数内で ``boto3.client("ssm" / "secretsmanager", region_name=region)``
を都度生成する。store 分岐ごとに **リテラルの service 名** と **service 専用の変数名**
(ssm / sm) を使うのは、`tests/test_permissions_sync.py` の boto3 AST 解析器が
「どの service のどの method を呼んでいるか」を静的に追跡できる形を保つため
(client を引数で受け取ると追跡が切れ、IAM 権限の網羅チェックが盲目になる)。
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from pocket.context import SecretsContext
    from pocket.settings import UserSecretSpec

# get_parameter / get_secret_value / delete_* が対象未存在時に返すエラーコード
_NOT_FOUND_CODES = ("ParameterNotFound", "ResourceNotFoundException")


class PutResult(enum.Enum):
    """put_stored_value がどの経路で書いたか。呼び出し側のメッセージ分岐用。"""

    CREATED = "created"  # ssm put_parameter / sm create_secret
    UPDATED = "updated"  # sm put_secret_value (既存 secret への上書き)


def put_stored_value(name: str, store: str, value: str, region: str) -> PutResult:
    """正準名 ``name`` に単一値を書き込む (ssm=SecureString 上書き / sm=create|put)。"""
    if store == "ssm":
        ssm = boto3.client("ssm", region_name=region)
        ssm.put_parameter(Name=name, Value=value, Type="SecureString", Overwrite=True)
        return PutResult.CREATED
    sm = boto3.client("secretsmanager", region_name=region)
    try:
        sm.create_secret(
            Name=name,
            SecretString=value,
            Tags=[{"Key": "Name", "Value": name}],
        )
        return PutResult.CREATED
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceExistsException":
            sm.put_secret_value(SecretId=name, SecretString=value)
            return PutResult.UPDATED
        raise


def read_stored_value(
    name: str, store: str, region: str, *, required: bool = False
) -> str | None:
    """正準名 ``name`` の値を読む。未存在 (NotFound) なら None。

    required=True なら NotFound を握らず ClientError をそのまま伝播させる
    (runtime の user secret 読み出しで欠落を即失敗させる用)。
    """
    try:
        if store == "ssm":
            ssm = boto3.client("ssm", region_name=region)
            return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"][
                "Value"
            ]
        sm = boto3.client("secretsmanager", region_name=region)
        return sm.get_secret_value(SecretId=name)["SecretString"]
    except ClientError as e:
        if not required and (
            e.response.get("Error", {}).get("Code", "") in _NOT_FOUND_CODES
        ):
            return None
        raise


def exists_stored_value(name: str, store: str, region: str) -> bool:
    """正準名 ``name`` が store に存在するか。

    値を読まずに存在だけ確認する (sm は describe_secret、ssm は get_parameter)。
    """
    try:
        if store == "ssm":
            ssm = boto3.client("ssm", region_name=region)
            ssm.get_parameter(Name=name)
        else:
            sm = boto3.client("secretsmanager", region_name=region)
            sm.describe_secret(SecretId=name)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code", "") in _NOT_FOUND_CODES:
            return False
        raise


def delete_stored_value(
    name: str,
    store: str,
    region: str,
    *,
    force_sm: bool = False,
    swallow_not_found: bool = False,
) -> None:
    """正準名 ``name`` を削除する。

    force_sm: SecretsManager 削除で ForceDeleteWithoutRecovery を付ける (即時削除)。
    swallow_not_found: 対象未存在 (NotFound) を握りつぶす。
    """
    try:
        if store == "ssm":
            ssm = boto3.client("ssm", region_name=region)
            ssm.delete_parameter(Name=name)
        elif force_sm:
            sm = boto3.client("secretsmanager", region_name=region)
            sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
        else:
            sm = boto3.client("secretsmanager", region_name=region)
            sm.delete_secret(SecretId=name)
    except ClientError as e:
        if swallow_not_found and (
            e.response.get("Error", {}).get("Code", "") in _NOT_FOUND_CODES
        ):
            return
        raise


class StoredUserSecretStore:
    """stored user secret (type 基準の正準名) への per-name I/O。

    値を生成する managed (pocket_store) と違い、値は外部 provisioner や
    `pocket <db> store-url` が焼く。CLI 側 (store-url の書込み・冪等判定、
    deploy 前 verify) と runtime 側 (`pocket.runtime.get_secrets`) の読み出しが
    この 1 実装を共有し、パス導出・store 分岐の不一致を構造的に防ぐ。
    `SecretsContext.user_store` から使う。
    """

    def __init__(self, context: SecretsContext) -> None:
        self.context = context

    def _effective_store(self, spec: UserSecretSpec) -> str:
        return spec.store or self.context.store

    def exists(self, spec: UserSecretSpec) -> bool:
        """spec.name (正準名) が store に存在するか (store-url の冪等判定用)。"""
        if spec.name is None:
            return False
        return exists_stored_value(
            spec.name, self._effective_store(spec), self.context.region
        )

    def read(self, spec: UserSecretSpec, *, required: bool = False) -> str | None:
        """spec.name (正準名) の値を読む。未 provision (NotFound) なら None。

        required=True なら NotFound も ClientError のまま伝播させる
        (runtime では欠落を即失敗させ、None を環境変数に流さない)。
        """
        if spec.name is None:
            if required:
                raise RuntimeError("user secret name is not resolved")
            return None
        return read_stored_value(
            spec.name,
            self._effective_store(spec),
            self.context.region,
            required=required,
        )

    def put(self, spec: UserSecretSpec, value: str) -> None:
        """spec.name (正準名) に単一値を書き込む。

        `pocket <db> store-url` から使う。pocket_store (managed 集約) ではなく
        user secret の type 基準導出名 ({pocket_key}-user/{type}) に直接 put する。
        読み側 exists / read と対称。
        """
        if spec.name is None:
            raise RuntimeError("user secret name is not resolved")
        put_stored_value(
            spec.name, self._effective_store(spec), value, self.context.region
        )

    def read_by_type(self, secret_type: str) -> str | None:
        """type 基準の正準パスを (consumer 宣言なしで) 構築して stored URL を読む。

        dual-declaration で consumer (DATABASE_URL) が別 backend を指していても、
        「その backend の stored URL」を type から直接引ける。store は secrets.store
        既定 (per-type override は宣言でしか表現できないため)。
        """
        name = self.context.stored_url_name(secret_type)
        return read_stored_value(name, self.context.store, self.context.region)

    def verify_provisioned(self) -> None:
        """type 付き user secret (stored mode) が deploy 前に provision 済みか検証する。

        computed (managed) と違い pocket は値を生成しないため、未 provision でも
        deploy は通り runtime まで遅延して落ちる。それを避けるため deploy 時に store を
        引いて存在を確認し、無ければ正準名を示して止める。管理 API は叩かない。
        """
        missing: list[str] = []
        for key, spec in self.context.user.items():
            # type 付き = stored mode のみ対象。name は from_settings で導出済み。
            if spec.type is None or spec.name is None:
                continue
            if not self.exists(spec):
                missing.append(
                    "  - %s (type=%s, store=%s): %s"
                    % (key, spec.type, self._effective_store(spec), spec.name)
                )
        if missing:
            raise RuntimeError(
                "stored mode の user secret が見つかりません。"
                "deploy 前に下記の secret を provision してください "
                "(値は接続 URL):\n" + "\n".join(missing)
            )
