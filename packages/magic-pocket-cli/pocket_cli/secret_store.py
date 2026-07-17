"""SecretsManager / SSM Parameter Store への単一値 I/O 共通ヘルパー。

`Rds._write/_read/_delete_credential_from_store` と
`Mediator._put/_read/_delete_stored_value` に重複していた
「create or put / get / delete (+NotFound 握り)」を集約する。

クライアントは各関数内で ``boto3.client("ssm" / "secretsmanager", region_name=region)``
を都度生成する。store 分岐ごとに **リテラルの service 名** と **service 専用の変数名**
(ssm / sm) を使うのは、`tests/test_permissions_sync.py` の boto3 AST 解析器が
「どの service のどの method を呼んでいるか」を静的に追跡できる形を保つため
(client を引数で受け取ると追跡が切れ、IAM 権限の網羅チェックが盲目になる)。
"""

from __future__ import annotations

import enum

import boto3
from botocore.exceptions import ClientError

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


def read_stored_value(name: str, store: str, region: str) -> str | None:
    """正準名 ``name`` の値を読む。未存在 (NotFound) なら None。"""
    try:
        if store == "ssm":
            ssm = boto3.client("ssm", region_name=region)
            return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"][
                "Value"
            ]
        sm = boto3.client("secretsmanager", region_name=region)
        return sm.get_secret_value(SecretId=name)["SecretString"]
    except ClientError as e:
        if e.response.get("Error", {}).get("Code", "") in _NOT_FOUND_CODES:
            return None
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
