import os
from functools import cache
from pathlib import Path

from pocket.general_context import GeneralContext

from .context import Context
from .settings import PocketSecretSpec
from .utils import get_stage, get_toml_path


@cache
def get_context(stage: str, path: str | Path) -> Context:
    return Context.from_toml(stage=stage, path=Path(path))


def _pocket_secret_to_envs(
    key: str, secrets: str | dict[str, str], spec: PocketSecretSpec
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


def get_secrets_from_secretsmanager(
    stage: str | None = None, path: str | Path | None = None
) -> dict:
    stage = stage or get_stage()
    if stage == "__none__":
        return {}
    path = path or get_toml_path()
    context = get_context(stage=stage, path=path)
    if (ac := context.awscontainer) is None:
        return {}
    if (sm := ac.secretsmanager) is None:
        return {}
    secrets = {}
    for key, value in sm.resource.user_secrets.items():
        secrets[key] = value
    for key, value in sm.resource.pocket_secrets.items():
        envs = _pocket_secret_to_envs(key, value, sm.pocket_secrets[key])
        secrets.update(envs)
    return secrets


def set_envs_from_secretsmanager(
    stage: str | None = None, path: str | Path | None = None
):
    if os.environ.get("POCKET_ENVS_SECRETSMANAGER_LOADED") == "true":
        return
    os.environ["POCKET_ENVS_SECRETSMANAGER_LOADED"] = "true"
    data = get_secrets_from_secretsmanager(stage, path)
    for key, value in data.items():
        os.environ[key] = value


def set_envs_from_aws_resources(
    stage: str | None = None,
    path: str | Path | None = None,
):
    if os.environ.get("POCKET_ENVS_AWS_RESOURCES_LOADED") == "true":
        return
    os.environ["POCKET_ENVS_AWS_RESOURCES_LOADED"] = "true"
    general_context = GeneralContext.from_toml(path=path)
    os.environ["POCKET_OBJECT_PREFIX"] = general_context.object_prefix
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
