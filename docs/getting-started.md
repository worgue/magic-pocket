# はじめに

このページでは、Djangoプロジェクトをmagic-pocketでAWS Lambda上にデプロイする手順を説明します。
Admin画面がLambda上で動くところまでを目標にします。

## 事前準備

以下の3つが必要です。

### Python パッケージマネージャー

magic-pocketはPyPIからインストール可能です。
以下の例では [uv](https://docs.astral.sh/uv/){:target="_blank"} を使います。他のツールを使う場合は、コマンドを適宜読み替えてください。

### AWS アカウント

[AWSアカウント](https://aws.amazon.com/){:target="_blank"} が必要です。

credentialsは `~/.aws/credentials` への設定を想定しています。
[boto3のドキュメント](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html#shared-credentials-file){:target="_blank"} を参考に設定してください。

### Neon アカウント

[Neonアカウント](https://neon.tech/){:target="_blank"} が必要です。

APIキーは環境変数 `NEON_API_KEY` で設定します。後の手順で `.env` に記述できます。

---

## 1. Djangoプロジェクトの作成

!!! warning "プロジェクト名の注意"
    - `pocket` をプロジェクト名に含めないでください。リソースのprefixと混同します。
    - 他のmagic-pocketプロジェクトと名前が被らないようにしてください。S3バケット名がコンフリクトします。

```bash
# Djangoプロジェクトを作成
uv init --python 3.12 your-project-name
cd your-project-name
uv add django
uv run django-admin startproject your_project_name .
uv run python manage.py runserver
```

localhost:8000 でDjangoが動くことを確認してください。

## 2. 依存パッケージの追加

```bash
uv add django-environ psycopg magic-pocket
```

!!! note "psycopgについて"
    macで開発している場合、`uv add "psycopg[binary]"` が必要になることがあります。

## 3. 初期設定の生成

`pocket django init` を実行すると、以下のファイルが自動生成されます。

- `pocket.toml` — デプロイ設定
- `pocket.Dockerfile` — Lambda用Dockerfile
- `settings.py` — 環境変数対応に書き換え（django-environが必要）
- `.env` — ローカル開発用の環境変数

!!! tip "事前にgit commitしておくと安心"
    `settings.py` が上書きされるので、差分を確認しやすくなります。
    `.gitignore` に `db.sqlite3` と `.env` を追加しておきましょう。

```bash
uv run pocket django init
```

### 生成される pocket.toml

```toml
[general]
region = "ap-southeast-1" # (1)!
stages = ["dev", "prd"] # (2)!

[s3] # (3)!

[neon] # (4)!
project_name = "dev-your-project-name"

[awscontainer] # (5)!
dockerfile_path = "pocket.Dockerfile"

[awscontainer.handlers.wsgi] # (6)!
command = "pocket.django.lambda_handlers.wsgi_handler"
[awscontainer.handlers.management] # (7)!
command = "pocket.django.lambda_handlers.management_command_handler"
timeout = 600

[dev.awscontainer.handlers.wsgi] # (8)!
apigateway = {}
[prd.awscontainer.handlers.wsgi]
apigateway = {}

[awscontainer.secrets.managed] # (9)!
SECRET_KEY = { type = "password", options = { length = 50 } }
DJANGO_SUPERUSER_PASSWORD = { type = "password", options = { length = 16 } }
DATABASE_URL = { type = "neon_database_url" }

[awscontainer.django.storages] # (10)!
default = { store = "s3", location = "media" }
staticfiles = { store = "s3", location = "static", static = true, manifest = true }
```

1. Neonが利用可能なAWSリージョンを指定してください。
2. devとprdの2ステージ構成です。
3. S3バケットを作成。バケット名はプロジェクト名+ステージ名から自動生成。
4. Neonデータベースを作成。
5. Lambdaコンテナの設定。
6. WSGIハンドラーのLambda関数を作成。
7. マネジメントコマンド実行用のLambda関数（timeout 600秒）。
8. dev/prd各環境にAPI Gatewayを設定。URLはAWSが自動生成。独自ドメインの場合は `apigateway = { domain = "example.com" }` と指定。
9. SECRET_KEY、スーパーユーザーパスワード、DB接続URLを自動生成しシークレットストアに保存。
10. S3上の `media` と `static` ディレクトリをDjangoのSTORAGESとして利用。

### 生成される .env

```bash
DEBUG=true
SECRET_KEY=ランダムに生成された値
DATABASE_URL=sqlite:///db.sqlite3
```

### settings.py への変更

以下のコードが追加され、環境変数とpocket.tomlから設定を読み込むようになります。

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

## 4. NEON_API_KEY の設定

`.env` に `NEON_API_KEY` を追加します。

```bash
# .env に追記
NEON_API_KEY=あなたのNeon APIキー
```

!!! info "NEON_API_KEY はローカルのデプロイ時のみ必要"
    Lambda環境では不要です。デプロイ時にNeonのDB情報を取得し、Secrets Managerに保存するため、Lambda側はSecrets Manager経由で接続します。

## 5. デプロイ

### dev環境

```bash
# デプロイ（インフラ構築 + コンテナイメージの作成・アップロード）
uv run pocket deploy --stage=dev

# Djangoの初期設定
uv run pocket django manage migrate --stage=dev
uv run pocket django manage collectstatic --noinput --stage=dev
uv run pocket django manage createsuperuser --username=admin --email=admin@example.com --noinput --stage=dev
```

自動生成されたシークレット（superuserのパスワード等）は以下で確認できます。

```bash
uv run pocket resource awscontainer secrets list --stage=dev --show-values
```

### prd環境

同じコマンドで `--stage=prd` に変えるだけです。環境・シークレットは全て別になります。

```bash
uv run pocket deploy --stage=prd
uv run pocket django manage migrate --stage=prd
uv run pocket django manage collectstatic --noinput --stage=prd
uv run pocket django manage createsuperuser --username=admin --email=admin@example.com --noinput --stage=prd
```

!!! success "デプロイ完了"
    API GatewayのURLにアクセスして、Django Admin画面が表示されれば成功です。
    URLは `pocket deploy` の出力に表示されます。
