# Django連携

Django アプリケーションでは、`pocket.django` モジュールを使って `pocket.toml` と AWS リソースから設定を取得します。

---

## STORAGES と CACHES

Django の `STORAGES` と `CACHES` は、`pocket.toml` の設定から自動生成されます。

```python
from pocket.django.utils import get_caches, get_storages

STORAGES = get_storages()
CACHES = get_caches()
```

これらの関数は、環境変数 `POCKET_STAGE` を参照して動作を切り替えます（[実行環境](runtime.md#pocket_stage) を参照）。

| 環境 | 参照する設定 |
|------|------------|
| Lambda（`POCKET_STAGE` あり） | `awscontainer.django.storages` / `awscontainer.django.caches` |
| ローカル（`POCKET_STAGE` なし） | `general.django_fallback.storages` / `general.django_fallback.caches` |

??? info "値が未設定の場合のデフォルト"
    **STORAGES**

    ```python
    {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    ```

    **CACHES**

    ```python
    {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    ```

---

## EMAIL_BACKEND

`pocket.toml` に `[ses]` セクションを設定すると、Django のメール送信バックエンドを SES に切り替えられます。

```python
from pocket.django.utils import get_email_backend

vars().update(get_email_backend())
```

| 環境 | 動作 |
|------|------|
| Lambda（`POCKET_STAGE` あり、`[ses]` 設定あり） | `django-ses` バックエンドを返す |
| Lambda（`POCKET_STAGE` あり、`[ses]` 設定なし） | 空 dict を返す（Django デフォルトのまま） |
| ローカル（`POCKET_STAGE` なし） | 空 dict を返す（Django デフォルトのまま） |

返される dict のキー:

| キー | 説明 |
|------|------|
| `EMAIL_BACKEND` | `"django_ses.SESBackend"` |
| `DEFAULT_FROM_EMAIL` | `[ses]` の `from_email` |
| `AWS_SES_REGION_NAME` | SES リージョン |
| `AWS_SES_CONFIGURATION_SET` | Configuration Set（設定時のみ） |

!!! note "django-ses のインストール"
    `django-ses` は `pip install magic-pocket[ses]` でインストールできます。

---

## 環境変数の登録 (set_envs)

`set_envs()` は、AWSリソースから取得した情報を環境変数に登録します。セットされる環境変数の一覧は「[実行環境](runtime.md#セットされる環境変数)」を参照してください。

```python
from pocket.django.runtime import set_envs

set_envs()

# この後で環境変数を読み取る
# SECRET_KEY = os.environ.get("SECRET_KEY")
```

追加で登録される情報:

- **Secrets Manager** — `pocket_secrets` で定義したシークレット（SECRET_KEY、DATABASE_URL など）
- **RDS シークレット** — `[rds]` が設定されている場合、AWS 管理シークレットから `DATABASE_URL` を自動構築
- **CloudFormation Output** — API GatewayのホストとCloudFrontドメインをDjangoの `ALLOWED_HOSTS` と `CSRF_TRUSTED_ORIGINS` に追加

!!! info "RDS の DATABASE_URL"
    `[rds]` を設定すると、Lambda 起動時に `POCKET_RDS_SECRET_ARN` 環境変数から RDS の AWS 管理シークレットを読み取り、
    `DATABASE_URL`（`postgres://postgres:{password}@{endpoint}:{port}/{dbname}`）を自動構築します。
    パスワードローテーションにも自動対応します。

!!! warning "settings.pyでの読み込みを忘れずに"
    `set_envs()` は環境変数を設定するだけです。
    `settings.py` 内で `os.environ` や `django-environ` 経由で値を読み込んでください。

    環境変数には型がありません。`DEBUG = os.environ.get("DEBUG")` のように取得すると文字列 `"False"` が真値になります。
    [django-environ](https://django-environ.readthedocs.io/) の利用を推奨します。

!!! warning "`set_envs_from_secrets()` ではなく `set_envs()` を使うこと"
    `pocket.runtime.set_envs_from_secrets()` はシークレットのみ設定します。
    Django プロジェクトでは `pocket.django.runtime.set_envs()` を使ってください。
    `set_envs()` はシークレットに加え、`ALLOWED_HOSTS` と `CSRF_TRUSTED_ORIGINS` も設定します。
    これを使わないと CloudFront 経由のアクセスで CSRF 検証エラー (403) が発生します。

!!! note "ローカル環境での動作"
    `POCKET_STAGE` が設定されていない場合、`set_envs()` は何もしません。
    ローカルでは `.env` ファイルと `django-environ` で環境変数を設定してください。

---

## Django settings の直接設定 {: #django-settings }

`pocket.toml` に記述したsettings値を、`settings.py` に直接読み込めます。

```toml
# ローカル
[general.django_fallback.settings]
CORS_ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:4173",
]

# dev
[dev.awscontainer.django.settings]
DEFAULT_FROM_EMAIL = '"Dev" <test@example.com>'
CORS_ALLOWED_ORIGINS = ["https://dev.example.com"]

# prd
[prd.awscontainer.django.settings]
DEFAULT_FROM_EMAIL = '"Production" <noreply@example.com>'
CORS_ALLOWED_ORIGINS = ["https://www.example.com"]
```

`settings.py` で読み込みます。

```python
from pocket.django.runtime import get_django_settings

vars().update(get_django_settings())
```

この関数も `POCKET_STAGE` で切り替わります。ローカルでは `general.django_fallback.settings`、Lambda上では対応するステージの `awscontainer.django.settings` が返されます。

---

## SQS経由のマネジメントコマンド実行

Lambda環境内からDjangoマネジメントコマンドを実行する場合、直接実行またはSQS経由で非同期実行できます。

```python
from pocket.django.utils import pocket_call_command

# 直接実行（デフォルト: SQSキューURLが設定されていれば自動でSQS経由）
pocket_call_command("my_command", args=["arg1"], kwargs={"key": "value"})

# 強制的に直接実行
pocket_call_command("my_command", force_direct=True)

# 強制的にSQS経由
pocket_call_command("my_command", force_sqs=True)
```

| 引数 | 型 | デフォルト | 説明 |
|------|------|----------|------|
| `command` | str | — | コマンド名 |
| `args` | list | `[]` | 位置引数 |
| `kwargs` | dict | `{}` | キーワード引数 |
| `force_direct` | bool | `False` | SQSを使わず直接実行 |
| `force_sqs` | bool | `False` | SQS経由を強制 |
| `queue_key` | str | `"sqsmanagement"` | SQSキューのキー名 |

---

## ステージ別ファイル配信 (managed_assets)

`favicon.ico` や `robots.txt` など、ステージごとに異なる内容を返したいファイルを Django view 経由で配信できます。

### ディレクトリ構成

プロジェクトルートに `managed_assets/` ディレクトリを作成します。CloudFront の `managed_assets` 設定と同じディレクトリ形式です。

```
managed_assets/
├── default/
│   ├── favicon.ico
│   └── robots.txt
└── sandbox/
    ├── favicon.ico
    └── robots.txt
```

`POCKET_STAGE` に応じたディレクトリが使用されます。ステージ名のディレクトリが存在しなければ `default/` にフォールバックします。

### urls.py への登録

```python
from pocket.django.urls import get_managed_assets_urls

urlpatterns = [
    # ...
    *get_managed_assets_urls(),
]
```

!!! note "CloudFront 構成との使い分け"
    Django view 経由の配信は Lambda を経由するため、CloudFront を使用している場合は `managed_assets` 設定（[設定リファレンス](configuration.md#managed_assets) を参照）を使うと S3 から直接配信でき、Lambda のコールドスタートを回避できます。同じディレクトリ形式なので、移行は設定の追加だけで完了します。

---

## settings.py の完全な例

以下は `django-environ` を利用した `settings.py` の典型的な構成です。

```python
import environ
from pathlib import Path

from pocket.django.runtime import set_envs, get_django_settings
from pocket.django.utils import get_caches, get_email_backend, get_storages

BASE_DIR = Path(__file__).resolve().parent.parent

# pocket.toml から STORAGES, CACHES, EMAIL を取得
STORAGES = get_storages()
CACHES = get_caches()
vars().update(get_email_backend())

# .env を読み込み（ローカル用）
environ.Env.read_env(BASE_DIR / ".env")
env = environ.Env(
    SECRET_KEY=str,
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
    CSRF_TRUSTED_ORIGINS=(list, []),
)

# AWS リソースから環境変数を登録（Lambda上のみ動作）
set_envs()

# 環境変数から読み込み
SECRET_KEY = env.str("SECRET_KEY")
DEBUG = env.bool("DEBUG")
DATABASES = {"default": env.db()}
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS")

# pocket.toml の django.settings を反映
vars().update(get_django_settings())
```
