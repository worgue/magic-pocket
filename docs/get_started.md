# Get Started

基本的な使い方は以下の通りです。

## Installation

```bash
pip install magic-pocket
```

## pocket.toml

プロジェクトのルートディレクトリに `pocket.toml` を作成します。
以下は、dev と prd 環境を持つ、django プロジェクトの例です。

```toml
[general]
region = "ap-southeast-1" # (1)!
stages = ["dev", "prd"] # (2)!

[s3] # (3)!
public_dirs = ["static"] # (4)!

[neon] # (5)!

[awscontainer] # (6)!
dockerfile_path = "pocket.Dockerfile" # (7)!

[awscontainer.handlers.wsgi] # (8)!
command = "pocket.django.lambda_handlers.wsgi_handler"
[awscontainer.handlers.management] # (9)!
command = "pocket.django.lambda_handlers.management_command_handler"
timeout = 600

[dev.awscontainer.handlers.wsgi] # (10)!
apigateway = {}
[prd.awscontainer.handlers.wsgi] # (11)!
apigateway = { domain = "example.com" }

[awscontainer.secretsmanager.pocket_secrets] # (12)!
SECRET_KEY = { type = "password", options = { length = 50 } }
DJANGO_SUPERUSER_PASSWORD = { type = "password", options = { length = 16 } }
DATABASE_URL = { type = "neon_database_url" }

[awscontainer.django.storages] # (13)!
default = { store = "s3", location = "media" }
staticfiles = { store = "s3", location = "static", static = true, manifest = true }

[prd.awscontainer.django.settings] # (14)!
DEFAULT_FROM_EMAIL = '"MagicPocket" <noreply@example.com>'
[dev.awscontainer.django.settings]
DEFAULT_FROM_EMAIL = '"MagicPocket Dev" <noreply-dev@example.com>'
```

1.  :man_raising_hand: ap-southeast-1 リージョンでリソースは作成されます。
2.  dev と prd の 2 つのステージが指定可能です。
3.  S3 バケットを作成します。名前は指定されていないので、プロジェクト名とステージ名から自動生成されます。
4.  作成されたバケットの static ディレクトリは公開されます
5.  neon データベースが作成されます。名前はプロジェクト名とステージ名から自動生成されます。
6.  Lambda コンテナイメージを作成します。ECR リポジトリ名はプロジェクト名とステージ名から自動生成されます。
7.  Lambda コンテナを作成する、Dockerfile のパスを指定します。
8.  wsgi という名前で、wsgi を実行する Lambda が作成されます。
9.  management という名前で、マネジメントコマンド を実行する Lambda が作成されます。timeout は 600 秒です。
10. dev の wsgi に apigateway が設定されます。URL は自動生成され、デプロイ時に表示されます。
11. prd の wsgi に apigateway が設定されます。ドメインは example.com です。
12. SECRET_KEY、DJANGO_SUPERUSER_PASSWORD、DATABASE_URL が自動生成され secretsmanager に保存されます
13. S3 に default と static のディレクトリが作成され、settings.py を通じて簡単に、django の settings.STORAGES 形式で読み込めます。
14. prd, dev 環境でそれぞれ、DEFAULT_FROM_EMAIL が設定され、settings.py から簡単に読み込めます

## django settings

`settings.py`からmagic-pocketによって管理されるリソースを読み込みます。

```python
from pocket.django.utils import get_caches, get_storages
from pocket.django.runtime import set_envs
from pocket.django.runtime import get_django_settings

STORAGES = get_storages()
CACHES = get_caches()
vars().update(get_django_settings().items())
set_envs()

# Read enviroment variables here
# SECRET_KEY = os.environ.get("SECRET_KEY")
# etc...
```

!!! warning "環境変数からsettings.pyに読み込むのを忘れないでください"
    作者はdjagno-environを利用することをお勧めします。

    どの様な方法でも構いませんが、環境変数には型がありません。
    os.environ.get("DEBUG")として、DEBUG=Falseを設定すると、デバッグモードになってしまいます。気をつけましょう。

## Dockerfile

`awscontainer.dockerfile_path`で指定した先に、Lambda対応の`Dockerfile` を作成します。
`requirements.lock`を持つdjangoプロジェクトの場合、以下の設定で動作します(1)。
{.annotate}

1. どういうライブラリが動かないのか、良く分かっていません。動かないライブラリがあれば、issueに投げてください。

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

# install git (1)
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

1. gitレポジトリからモジュールをダウンロードする必要がある場合に必要です。なければ不要です。

## Deploy

### dev
ここまでの設定で、`SECRET_KEY`、`DJANGO_SUPERUSER_PASSWORD`、`DATABASE_URL` は、SecretsManagerに保存され`settings.py`から読み込まれます。
そのため、以下のコマンドでdev環境をデプロイと、djangoの初期設定を行います。

```bash
pocket deploy --stage=dev
pocket django manage migrate --stage=dev
pocket django manage collectstatic --noinput --stage=dev
pocket django manage createsuperuser --username=admin --email=admin@example.com --noinput --stage=dev
```


`DJANGO_SUPERUSER_PASSWORD`を含む自動生成された内容は、以下で取得できます。

```bash
pocket resource awscontainer secretsmanager list --stage dev --show-values
```

### prd
devをprdに変えるだけです。環境がコンフリクトすることはありません。
```bash
pocket deploy --stage=prd
pocket django manage migrate --stage=prd
pocket django manage collectstatic --noinput --stage=prd
pocket django manage createsuperuser --username=admin --email=admin@example.com --noinput --stage=prd
```
