import os

from pocket.context import Context


def set_user_secrets_from_secretsmanager(stage: str | None = None):
    stage = stage or os.environ.get("POCKET_STAGE")
    if not stage:
        return
    context = Context.from_toml(stage=stage, filters=["awscontainer", "region"])
    if secretsmanager := context.awscontainer and context.awscontainer.secretsmanager:
        for key, value in secretsmanager.resource.resolved_secrets.items():
            os.environ[key] = value


def set_env_from_resources(stage: str | None = None):
    stage = stage or os.environ.get("POCKET_STAGE")
    if not stage:
        return
    context = Context.from_toml(stage=stage)
    os.environ["POCKET_RESOURCES_ENV_LOADED"] = "true"
    if neon := context.neon:
        os.environ["DATABASE_URL"] = neon.resource.database_url
    if awscontainer := context.awscontainer:
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
