# Tutorial - Simple Django Project
ここでは、Lambda + Neon + S3でDjangoプロジェクトを動かします。
Admin画面を確認することが目標です。

## 事前準備
rye, AWS, Neon

??? rye
    magic-pocketは、PyPIからインストール可能です。
    ここでは、[rye](https://rye.astral.sh/){:target="_blank"}を利用します。
    他のツールを使う場合、コマンドを適宜調整してください。
    ryeを使ったことがない場合、uvの方が良いかもしれません。
    ryeからuvへの移行方法が分かり次第、ドキュメントはuvに変更される予定です。

??? AWS
    [AWSアカウント](https://aws.amazon.com/){:target="_blank"}が必要です。

    credentialsは、`~/.aws/credentials`への設定を想定しています。

    boto3からAWSを操作しますが、credentialsを直接読み込むことはしないので、[boto3の設定](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html#shared-credentials-file){:target="_blank"}を参考に設定してください。

!!! Neon
    [Neonアカウント](https://neon.tech/){:target="_blank"}が必要です。

    APIキーは、`NEON_API_KEY`という環境変数で設定してください。

    チュートリアルに沿えば、`.env`で設定できます。

## Djangoプロジェクトの作成
!!! warning annotate "プロジェクト名について"
    `pocket`をプロジェクト名に含めない
    :    `pocket`というprefixをつけてリソースを作成するため、prefixかプロジェクト名か分からず、混乱します。

    他のmagic-pocketプロジェクトとコンフリクトさせない
    :   自分の名前などを入れてください。
        プロジェクト名から自動生成させるS3バケット名(1)がコンフリクトしてエラーになります。
        `pocket`などの名前は動かないかもしれません(2)。


1. `pocket`というプレフィックスが付くので、通常はコンフリクトしません。この値は、`pocket.toml`で直接指定も可能ですが、チュートリアルでは、プロジェクト名で対応してください。
2. 実際は、`pocket`という名称はS3の問題ではなく動きません。magic-pocketのpocketディレクトリよりも、プロジェクトディレクトリがimportで優先されるためです。

```bash
# global django (1)
rye install django
django-admin startproject `your-project-name`
cd `your-project-name`
rye pin 3.12
rye init --virtual
```

1. django-adminコマンドを利用するため、グローバルにdjangoをインストールします。

## Djangoのインストールとrunserver
```bash
# project django(1)
rye add django
python manage.py migrate
# runserver(2)
python manage.py runserver
```

1. プロジェクトにdjangoを追加します。
2. localhost:8000でdjangoが動いていることを確認してください。

## django-environとpsycopgのインストール
```bash
rye add django-environ psycopg
```

??? warning "Lambda環境はデフォルトでlinux/amd64です"
    macで開発している場合、`rye add "psycopg[binary]"`とする必要があるかもしれません。


## maginc-pocketのインストール
```bash
rye add magic-pocket
```

## pocket django initの実行
以下のコマンドは、シンプルなデプロイ設定を生成します。

!!! info annotate "実行前のコミットをお勧めします"
    `settings.py`の差分確認(1)のため、git commitをしておくことをお勧めします。
    チュートリアル通りに進めていれば、`.gitignore`には`db.sqlite3`を追加してください。

    コマンド実行時に生成される`.env`も、ついでに`.gitignore`に追加しておくと良いでしょう。

1. 以下に差分は記載しますが、手元で確認できたほうが安心かと思います。

```bash
rye run pocket django init
```

このコマンドにより、以下のファイルが生成されます。

- `pocket.toml`, `pocket.Dockerfile`, `.env`が生成され、新規保存されます。
- `settings.py`が生成され、上書きされます。

??? warning "チュートリアル通りに実行していない場合の注意点"
    **djagno-environ**

    :   django-environがない場合、`.env`と`settings.py`は作成されません。
        環境変数としてしか取得できないデータがあるので、チュートリアルでは、django-environを利用した`settings.py`を作成することをお勧めします。

### pocket.toml
ap-southeast-1リージョン(1)で、devとprdの2つのステージを持つプロジェクトの設定ファイルです。
{.annotate}

1. Neonが存在するリージョンを設定してください。


```toml
[general]
region = "ap-southeast-1" # (1)!
stages = ["dev", "prd"] # (2)!

[s3] # (3)!

[neon] # (4)!

[awscontainer] # (5)!
dockerfile_path = "pocket.Dockerfile" # (6)!

[awscontainer.handlers.wsgi] # (7)!
command = "pocket.django.lambda_handlers.wsgi_handler"
[awscontainer.handlers.management] # (8)!
command = "pocket.django.lambda_handlers.management_command_handler"
timeout = 600

[dev.awscontainer.handlers.wsgi] # (9)!
apigateway = {}
[prd.awscontainer.handlers.wsgi]
apigateway = {}

[awscontainer.secretsmanager.pocket_secrets] # (10)!
SECRET_KEY = { type = "password", options = { length = 50 } }
DJANGO_SUPERUSER_PASSWORD = { type = "password", options = { length = 16 } }
DATABASE_URL = { type = "neon_database_url" }

[awscontainer.django.storages] # (11)!
default = { store = "s3", location = "media" }
staticfiles = { store = "s3", location = "static", static = true, manifest = true }
```

1.  :man_raising_hand: `ap-southeast-1`リージョンでリソースは作成されます。Neonが使えるリージョンを指定してください。
2.  `dev`と`prd`の2つのステージが指定可能です。
3.  S3バケットを作成します。名前は指定されていないので、プロジェクト名とステージ名から自動生成されます。
4.  Neonデータベースが作成されます。名前はプロジェクト名とステージ名から自動生成されます。
5.  Lambdaコンテナイメージを作成します。ECRリポジトリ名はプロジェクト名とステージ名から自動生成されます。
6.  Lambdaコンテナを作成する、Dockerfileのパスを指定します。
7.  `wsgi`という名前で、wsgiを実行するLambda関数が作成されます。
8.  `management`という名前で、マネジメントコマンドを実行するLambda関数が作成されます。timeoutは600秒です。
9.  `dev`と`prd`のwsgiにapigatewayが設定されます。URLはAWS側で自動生成されます。項目を分けているのは、`apigateway = { domain = "example.com" }`のように、ドメインを指定をするためです。Route53のホストゾーンがあれば、自動設定可能です。外部の設定を用いる場合、CloudFomationがDNSレコードの認証待ちになります。
10. SECRET_KEY、DJANGO_SUPERUSER_PASSWORD、DATABASE_URL が自動生成され secretsmanager に保存されます
11. S3 に default と static のディレクトリが作成され、settings.py を通じて簡単に、django の settings.STORAGES 形式で読み込めます。

### pocket.Dockerfile
Lambda対応のDockerfileを生成します(1)。
{.annotate}

1. どういうライブラリが動かないのか、良く分かっていません。動かないライブラリがあれば、issueに投げてください。

```Dockerfile
ARG PYTHON_VERSION=3.12
FROM public.ecr.aws/docker/library/python:${PYTHON_VERSION}-slim AS base

FROM base AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/venv \
    UV_PROJECT_ENVIRONMENT=/venv \
    PATH="/venv/bin:${PATH}"
WORKDIR /app

RUN uv venv $VIRTUAL_ENV
COPY uv.lock pyproject.toml ./
RUN uv sync --frozen --no-dev --no-install-project

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

### .env
!!! tip "ローカル環境用の設定ファイルです"
    このファイルは、ローカルでDjangoを動かすための設定ファイルです。
    デプロイ時には、これらの変数は`secretsmanager`経由で取得されるため、デプロイ環境を動かすだけなら、`.env`は不要です。

以下の内容で生成されます。


```bash
DEBUG=true
# secret (1)
SECRET_KEY=`random-secret-key`
DATABASE_URL=sqlite:///db.sqlite3
```

1. Djangoの`SECRET_KEY`生成の仕組みを利用して、毎回異なる値が出力されます。

### settings.py
以下の修正が適用された`settings.py`が生成されます。

- `SECRET_KEY`, `ALLOWED_HOSTS`, `DATABASES`, `DEBUG`を環境変数から読み込ませるように修正します。デプロイ時には`SecretsManager`経由、ローカルでは`.env`経由で取得します。
- `STORAGES`, `CACHES`を`pocket.toml`から読み込む形に修正します。

追加されるコードは以下になります。

```python
from pocket.django.runtime import set_envs
from pocket.django.utils import get_caches, get_storages

STORAGES = get_storages()
CACHES = get_caches()

environ.Env.read_env(BASE_DIR / ".env")
env = environ.Env(
    SECRET_KEY=str,
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
)

set_envs()
SECRET_KEY = env.str("SECRET_KEY")
DEBUG = env.bool("DEBUG")
DATABASES = {"default": env.db()}
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS")
```

## deploy
!!! info annotate "NEON_API_KEYがローカル環境に必要です"
    deployコマンドを実行する環境には、`NEON_API_KEY`を設定する必要があります。(1)

    $ NEON_API_KEY=`key` rye run pocket deploy --stage=dev

    という形でCLI実行時に指定も可能ですが、magic-pocketはデフォルトで`.env`を見に行きます。
    ここまでで、.envが出来ているはずなので、.envに設定するのが楽だと思います。

    ??? warning "django-environとmagic-pocketの`.env`読み込み機能は別実装です"
        `.env`があるので追加すれば良いのですが、このチュートリアルで`settings.py`が`.env`を読みこんでいるのは、django-environへの設定です。
        magic-pocketは、django-environや上記`settings.py`の設定がない場合でも、`NEON_API_KEY`を取得するためデフォルトで`.env`を利用します。

1. Lambda環境では不要です。magic-pocketはデプロイ時にNEONのデータベース情報を読み込み、SecretsManagerに登録します。Lambda環境では、その値を環境変数として接続情報を取得できます。

### dev
以下のコマンドでdev環境へのデプロイと、djangoの初期設定を行います。

```bash
pocket deploy --stage=dev
pocket django manage migrate --stage=dev
pocket django manage collectstatic --noinput --stage=dev
pocket django manage createsuperuser --username=admin --email=admin@example.com --noinput --stage=dev
```

`SECRET_KEY`、`DJANGO_SUPERUSER_PASSWORD`、`DATABASE_URL` は、自動生成されSecretsManagerに保存されます。その値は`settings.py`で取得されます。

自動生成されたシークレットは、以下で確認できます。

```bash
pocket resource awscontainer secretsmanager list --stage dev --show-values
```

### prd
devをprdに変えるだけです。環境やシークレットは全て別になります。

```bash
pocket deploy --stage=prd
pocket django manage migrate --stage=prd
pocket django manage collectstatic --noinput --stage=prd
pocket django manage createsuperuser --username=admin --email=admin@example.com --noinput --stage=prd
```
