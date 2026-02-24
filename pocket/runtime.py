import os
from functools import cache
from pathlib import Path

from pocket.general_context import GeneralContext

from .context import Context
from .resources.aws.secretsmanager import SecretsManager
from .resources.aws.ssm import SsmStore
from .settings import ManagedSecretSpec
from .utils import get_stage, get_toml_path


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
        hosts = []
        for lambda_key, host in context.awscontainer.resource.hosts.items():
            if host:
                hosts.append(host)
                os.environ["POCKET_%s_HOST" % lambda_key.upper()] = host
                os.environ["POCKET_%s_ENDPOINT" % lambda_key.upper()] = (
                    "https://%s" % host
                )
        os.environ["POCKET_HOSTS"] = "".join(hosts)
        for lambda_key, queueurl in context.awscontainer.resource.queueurls.items():
            if queueurl:
                os.environ["POCKET_%s_QUEUEURL" % lambda_key.upper()] = queueurl
    else:
        os.environ["POCKET_HOSTS"] = ""
