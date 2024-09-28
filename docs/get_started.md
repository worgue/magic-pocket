# Get Started

基本的な使い方は以下の通りです。

- [Installation](#installation)
- [Add pocket.toml](#add-pockettoml)
- [Update django settings](#update-django-settings)
- [Add Dockerfile](#add-dockerfile)
- [Deploy](#deploy)

## Installation

```bash
pip install magic-pocket
```

## Add pocket.toml

プロジェクトのルートディレクトリに `pocket.toml` を作成します。
以下は、dev と prd 環境を持つ、django プロジェクトの例です。

```toml
[general]
region = "ap-southeast-1"
stages = ["dev", "prd"]

[s3]
public_dirs = ["static"]

[neon]

[awscontainer]
dockerfile_path = "Dockerfile"

[awscontainer.handlers.wsgi]
command = "pocket.django.lambda_handlers.wsgi_handler"
[awscontainer.handlers.management]
command = "pocket.django.lambda_handlers.management_command_handler"
timeout = 600

[dev.awscontainer.handlers.wsgi]
apigateway = {}
[prd.awscontainer.handlers.wsgi]
apigateway = { domain = "example.com" }

[awscontainer.secretsmanager.pocket_secrets]
SECRET_KEY = { type = "password", options = { length = 50 } }
DJANGO_SUPERUSER_PASSWORD = { type = "password", options = { length = 16 } }
DATABASE_URL = { type = "neon_database_url" }

[awscontainer.django.storages]
default = { store = "s3", location = "media" }
staticfiles = { store = "s3", location = "static", static = true, manifest = true }

[prd.awscontainer.django.settings]
DEFAULT_FROM_EMAIL = '"MagicPocket" <noreply@example.com>'
[dev.awscontainer.django.settings]
DEFAULT_FROM_EMAIL = '"MagicPocket Dev" <noreply-dev@example.com>'
```

こちらの yaml は、以下の環境を宣言します。

- ap-southeast-1 リージョンでリソースは作成されます
- dev と prd の 2 つのステージが指定可能です
- S3 のバケット名は指定されていないので、環境ごとに自動生成され、static ディレクトリは公開されます
- neon データベースが作成されます
- Dockerfile を元にした lambda が作成されます
- wsgi 実行用と management コマンド実行用の lambda が、同じイメージから作成されます
- wsgi には、dev でも prd でも apigateway が設定され、prd ではドメインが設定されます
- ドメインが設定されない apigateway は、自動で作られた API Gateway の URL が使われ、deploy 時に表示されます
- SECRET_KEY、DJANGO_SUPERUSER_PASSWORD、DATABASE_URL が自動生成され secretsmanager に保存されます
- S3 に default と static のディレクトリが作成され、settings.py を通じて簡単に、django の settings.STORAGES 形式で読み込めます
- prd, dev 環境でそれぞれ、DEFAULT_FROM_EMAIL が設定され、settings.py から簡単に読み込めます

読んで理解するままの環境が作成されるべきだと思うので、分かりにくい記述があれば、issue に投げてください。

## Update django settings

`settings.py` に以下の設定を追加します。

```python
from pocket.django.runtime import get_django_settings, set_django_env
from pocket.django.utils import get_caches, get_storages
from pocket.runtime import (
    set_env_from_resources,
    set_user_secrets_from_secretsmanager,
)

set_user_secrets_from_secretsmanager()
set_env_from_resources()
set_django_env()
vars().update(get_django_settings().items())
STORAGES = get_storages()
CACHES = get_caches()
```

## Add Dockerfile

awscontainer.dockerfile_path で指定した先に、lambda 対応の`Dockerfile` を作成します。
利用用途によると思いますが、requirements.lock を持つ django プロジェクトの場合、以下の設定で動作すると思います。
git 管理された python モジュールが requirements.lock に記述されていることを前提としていますが、なければ、git のインストールは不要です。

```Dockerfile
ARG PYTHON_VERSION=3.12
FROM public.ecr.aws/docker/library/python:${PYTHON_VERSION}-slim AS base

FROM base AS builder
ARG REQUIREMENTS=requirements.lock
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=100 \
    VIRTUAL_ENV=/venv \
    PATH="/venv/bin:${PATH}"
WORKDIR /app

RUN apt-get update && apt-get install -y git

RUN python -m venv $VIRTUAL_ENV
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=bind,source=${REQUIREMENTS},target=${REQUIREMENTS} \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    /venv/bin/pip install -r ${REQUIREMENTS}

FROM base AS final
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/venv \
    PATH="/venv/bin:${PATH}"
WORKDIR /app
COPY --from=builder /venv /venv
COPY . .

ENTRYPOINT [ "/venv/bin/python", "-m", "awslambdaric" ]
```

## Deploy

### dev

以下のコマンドで dev 環境をデプロイと、初期設定を行います。

```bash
pocket deploy --stage=dev
pocket django manage collectstatic --noinput --stage=dev
pocket django manage createsuperuser --username=admin --email=admin@example.com --noinput --stage=dev
```

上記の yaml では、SECRET_KEY、DJANGO_SUPERUSER_PASSWORD、DATABASE_URL は、secretsmanager に保存されます。内容は、上記 settings.py から読み込まれます。

DJANGO_SUPERUSER_PASSWORD を含む自動生成された内容は、以下のコマンドで取得できます。

```bash
pocket resource awscontainer secretsmanager list --stage dev --show-values
```

### prd

上記の dev を prd に変えるだけで、prd 環境にデプロイできます。
