from __future__ import annotations

import os
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

import boto3

from pocket.general_context import GeneralContext

from .context import Context
from .resources.aws.secretsmanager import SecretsManager
from .resources.aws.ssm import SsmStore
from .settings import ManagedSecretSpec
from .utils import get_stage, get_toml_path

if TYPE_CHECKING:
    from .context import AwsContainerContext


@cache
def get_context(stage: str, path: str | Path) -> Context:
    return Context.from_toml(stage=stage, path=Path(path))


def _pocket_secret_to_envs(
    key: str, secrets: str | dict[str, str], spec: ManagedSecretSpec
) -> dict[str, str]:
    if isinstance(secrets, str):
        return {key: secrets}
    elif spec.type == "rsa_pem_base64":
        pem_suffix = spec.options["pem_base64_environ_suffix"]
        pub_suffix = spec.options["pub_base64_environ_suffix"]
        return {
            f"{key}{pem_suffix}": secrets["pem"],
            f"{key}{pub_suffix}": secrets["pub"],
        }
    raise Exception(f"Unsupported pocket secret spec: {spec}")


def get_secrets(stage: str | None = None, path: str | Path | None = None) -> dict:
    stage = stage or get_stage()
    if stage == "__none__":
        return {}
    path = path or get_toml_path()
    context = get_context(stage=stage, path=path)
    if (ac := context.awscontainer) is None:
        return {}
    if (sc := ac.secrets) is None:
        return {}
    secrets = {}
    # managed: pocket_store経由で自動ディスパッチ (SM/SSM)
    for key, value in sc.pocket_store.secrets.items():
        envs = _pocket_secret_to_envs(key, value, sc.managed[key])
        secrets.update(envs)
    # user: 各specのstoreに応じてSM/SSMクライアントを使い分け
    sm_client: SecretsManager | None = None
    ssm_client: SsmStore | None = None
    for key, spec in sc.user.items():
        effective_store = spec.store or sc.store
        if effective_store == "sm":
            if sm_client is None:
                sm_client = SecretsManager(sc)
            res = sm_client.client.get_secret_value(SecretId=spec.name)
            secrets[key] = res["SecretString"]
        else:
            if ssm_client is None:
                ssm_client = SsmStore(sc)
            res = ssm_client.client.get_parameter(Name=spec.name, WithDecryption=True)
            secrets[key] = res["Parameter"]["Value"]
    return secrets


def set_envs_from_secrets(stage: str | None = None, path: str | Path | None = None):
    if os.environ.get("POCKET_ENVS_SECRETS_LOADED") == "true":
        return
    os.environ["POCKET_ENVS_SECRETS_LOADED"] = "true"
    data = get_secrets(stage, path)
    for key, value in data.items():
        os.environ[key] = value


# 後方互換エイリアス
get_secrets_from_secretsmanager = get_secrets
set_envs_from_secretsmanager = set_envs_from_secrets


def _get_host(ac_context: AwsContainerContext, key: str) -> str | None:
    """CFN stack output と context data から host を取得"""
    handler = ac_context.handlers[key]
    if handler.apigateway is None:
        return None
    if handler.apigateway.domain:
        return handler.apigateway.domain
    apiendpoint_key = key.capitalize() + "ApiEndpoint"
    stack_name = f"{ac_context.slug}-container"
    cfn = boto3.client("cloudformation", region_name=ac_context.region)
    try:
        res = cfn.describe_stacks(StackName=stack_name)
        outputs = res["Stacks"][0].get("Outputs", [])
        for output in outputs:
            if output["OutputKey"] == apiendpoint_key:
                return output["OutputValue"][len("https://") :]
    except cfn.exceptions.ClientError:
        pass
    return None


def _get_hosts(ac_context: AwsContainerContext) -> dict[str, str | None]:
    """全 handler の hosts を取得"""
    data: dict[str, str | None] = {}
    for key, handler in ac_context.handlers.items():
        if handler.apigateway is not None:
            data[key] = _get_host(ac_context, key)
    return data


def _get_queueurls(ac_context: AwsContainerContext) -> dict[str, str | None]:
    """SQS get_queue_url で queue URL を取得"""
    data: dict[str, str | None] = {}
    for key, handler in ac_context.handlers.items():
        if handler.sqs:
            try:
                res = boto3.client("sqs").get_queue_url(QueueName=handler.sqs.name)
                data[key] = res["QueueUrl"]
            except boto3.client("sqs").exceptions.QueueDoesNotExist:
                data[key] = None
        else:
            data[key] = None
    return data


def set_envs_from_aws_resources(
    stage: str | None = None,
    path: str | Path | None = None,
):
    if os.environ.get("POCKET_ENVS_AWS_RESOURCES_LOADED") == "true":
        return
    os.environ["POCKET_ENVS_AWS_RESOURCES_LOADED"] = "true"
    general_context = GeneralContext.from_toml(path=path)
    os.environ["POCKET_NAMESPACE"] = general_context.namespace
    os.environ["POCKET_PREFIX_TEMPLATE"] = general_context.prefix_template
    os.environ["POCKET_PROJECT_NAME"] = general_context.project_name
    os.environ["POCKET_REGION"] = general_context.region
    stage = stage or get_stage()
    if stage == "__none__":
        return {}
    path = path or get_toml_path()
    context = get_context(stage=stage, path=path)
    if context.awscontainer:
        hosts_map = _get_hosts(context.awscontainer)
        hosts = []
        for lambda_key, host in hosts_map.items():
            if host:
                hosts.append(host)
                os.environ["POCKET_%s_HOST" % lambda_key.upper()] = host
                os.environ["POCKET_%s_ENDPOINT" % lambda_key.upper()] = (
                    "https://%s" % host
                )
        os.environ["POCKET_HOSTS"] = "".join(hosts)
        queueurls_map = _get_queueurls(context.awscontainer)
        for lambda_key, queueurl in queueurls_map.items():
            if queueurl:
                os.environ["POCKET_%s_QUEUEURL" % lambda_key.upper()] = queueurl
    else:
        os.environ["POCKET_HOSTS"] = ""
