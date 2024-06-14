import os
from pathlib import Path

from .context import Context
from .utils import get_stage, get_toml_path


def get_user_secrets_from_secretsmanager(
    stage: str | None = None, path: str | Path | None = None
) -> dict:
    stage = stage or get_stage()
    if stage == "__none__":
        return {}
    path = path or get_toml_path()
    context = Context.from_toml(stage=stage, path=path)
    if (ac := context.awscontainer) is None:
        return {}
    if (sm := ac.secretsmanager) is None:
        return {}
    secrets = {}
    for key, value in sm.resource.user_secrets.items():
        secrets[key] = value
    for key, value in sm.resource.pocket_secrets.items():
        secrets[key] = value
    return secrets


def set_user_secrets_from_secretsmanager(
    stage: str | None = None, path: str | Path | None = None
):
    for key, value in get_user_secrets_from_secretsmanager(stage, path).items():
        os.environ[key] = value


def set_env_from_resources(
    stage: str | None = None,
    path: str | Path | None = None,
    use_neon=False,
    use_awscontainer=True,
):
    stage = stage or get_stage()
    if stage == "__none__":
        return
    path = path or get_toml_path()
    context = Context.from_toml(stage=stage, path=path)
    os.environ["POCKET_RESOURCES_ENV_LOADED"] = "true"
    if (neon := context.neon) and use_neon:
        # secretmanager.pocket in pocket.toml is preferred.
        # e.g) DATABASE_URL = { type = "neon_database_url" }
        os.environ["DATABASE_URL"] = neon.resource.database_url
    if (awscontainer := context.awscontainer) and use_awscontainer:
        hosts = []
        for lambda_key, host in awscontainer.resource.hosts.items():
            if host:
                hosts.append(host)
                os.environ["POCKET_%s_HOST" % lambda_key.upper()] = host
                os.environ["POCKET_%s_ENDPOINT" % lambda_key.upper()] = (
                    "https://%s" % host
                )
        os.environ["POCKET_HOSTS"] = "".join(hosts)
        for lambda_key, queueurl in awscontainer.resource.queueurls.items():
            if queueurl:
                os.environ["POCKET_%s_QUEUEURL" % lambda_key.upper()] = queueurl
    else:
        os.environ["POCKET_HOSTS"] = ""
