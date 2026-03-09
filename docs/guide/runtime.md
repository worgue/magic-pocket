# 実行環境とDjango連携

Lambda上のDjangoアプリケーションは、`pocket.toml` とAWSリソースから設定情報を取得します。

---

## STORAGES と CACHES

Djangoの `STORAGES` と `CACHES` は、`pocket.toml` の設定から自動生成されます。

```python
from pocket.django.utils import get_caches, get_storages

STORAGES = get_storages()
CACHES = get_caches()
```

これらの関数は、環境変数 `POCKET_STAGE` を参照して動作を切り替えます。

!!! info "`POCKET_STAGE` の役割"
    `POCKET_STAGE` は2つの用途で使用されます。

    - **Lambda ランタイム**: Lambda環境ではCloudFormationにより自動設定され、実行環境のステージを判定します
    - **CLI デフォルトステージ**: `pocket` コマンドの `--stage` オプションのデフォルト値として参照されます（[CLI](cli.md#pocket_stage-環境変数) を参照）

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

`set_envs()` は、AWSリソースから取得した情報を環境変数に登録します。

```python
from pocket.django.runtime import set_envs

set_envs()

# この後で環境変数を読み取る
# SECRET_KEY = os.environ.get("SECRET_KEY")
```

登録される情報:

- **Secrets Manager** — `pocket_secrets` で定義したシークレット（SECRET_KEY、DATABASE_URL など）
- **RDS シークレット** — `[rds]` が設定されている場合、AWS 管理シークレットから `DATABASE_URL` を自動構築
- **CloudFormation Output** — API GatewayのホストをDjangoの `ALLOWED_HOSTS` に追加

!!! info "RDS の DATABASE_URL"
    `[rds]` を設定すると、Lambda 起動時に `POCKET_RDS_SECRET_ARN` 環境変数から RDS の AWS 管理シークレットを読み取り、
    `DATABASE_URL`（`postgres://postgres:{password}@{endpoint}:{port}/{dbname}`）を自動構築します。
    パスワードローテーションにも自動対応します。

!!! warning "settings.pyでの読み込みを忘れずに"
    `set_envs()` は環境変数を設定するだけです。
    `settings.py` 内で `os.environ` や `django-environ` 経由で値を読み込んでください。

    環境変数には型がありません。`DEBUG = os.environ.get("DEBUG")` のように取得すると文字列 `"False"` が真値になります。
    [django-environ](https://django-environ.readthedocs.io/) の利用を推奨します。

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

## SPA トークン認証 {: #spa-トークン認証 }

CloudFront 配信の SPA にログイン必須機能を追加する場合、`pocket.django.spa_auth` モジュールを使用します。
HMAC-SHA256 トークンを Cookie にセットし、CloudFront Function で検証します。

### トークンの生成と検証

```python
from pocket.django.spa_auth import generate_token, verify_token

# トークン生成（ログイン時）
token = generate_token("user123")  # デフォルト有効期限: 7日

# トークン検証（任意のバックエンド処理で）
user_id = verify_token(token)  # 有効なら user_id、無効なら None
```

シークレットは環境変数 `SPA_TOKEN_SECRET` から自動取得されます。
テスト時は `secret` パラメータで明示指定できます。

### ログイン・ログアウト

Django の View でレスポンスに Cookie をセットします。

```python
from django.http import HttpResponseRedirect
from pocket.django.spa_auth import spa_login, spa_logout

def login_view(request):
    # Django 認証でユーザーを検証後...
    response = HttpResponseRedirect(request.GET.get("next", "/"))
    spa_login(response, str(request.user.id))
    return response

def logout_view(request):
    response = HttpResponseRedirect("/")
    spa_logout(response)
    return response
```

### API リファレンス

| 関数 | 引数 | 戻り値 | 説明 |
|------|------|--------|------|
| `generate_token(user_id)` | `user_id: str`, `secret: str\|None`, `max_age: int` | `str` | HMAC-SHA256 トークンを生成 |
| `verify_token(token)` | `token: str`, `secret: str\|None` | `str\|None` | トークンを検証し、有効なら user_id を返す |
| `spa_login(response, user_id)` | `response`, `user_id: str`, `secret: str\|None`, `max_age: int` | — | レスポンスにトークン Cookie をセット |
| `spa_logout(response)` | `response` | — | レスポンスからトークン Cookie を削除 |

- `secret` を省略すると `os.environ["SPA_TOKEN_SECRET"]` を使用します
- `max_age` のデフォルトは `604800`（7日間）です
- Cookie 名は `pocket-spa-token` で、`HttpOnly`, `Secure`, `SameSite=Lax` が設定されます

### Rust (Loco) での利用

Rust アプリケーションでは `pocket-spa-auth` crate を使用できます。

```rust
use pocket_spa_auth::{generate_token, verify_token, login_cookie_value, logout_cookie_value};

let secret = std::env::var("SPA_TOKEN_SECRET").unwrap();

// トークン生成
let token = generate_token("user123", &secret, 604800);

// トークン検証
if let Some(user_id) = verify_token(&token, &secret) {
    println!("認証成功: {}", user_id);
}

// Cookie 値の生成
let set_cookie = login_cookie_value(&token, 604800);
let delete_cookie = logout_cookie_value();
```

---

## Rust (Loco) ランタイム {: #rust-runtime }

Rust アプリケーション（Loco など）では、`magic-pocket-rs` crate を使って環境変数をセットアップします。
Django の `set_envs()` に相当する機能を Rust で提供します。

### セットアップ

`Cargo.toml` に依存を追加します。

```toml
[dependencies]
magic-pocket-rs = { git = "https://github.com/worgue/magic-pocket.git" }
lambda_http = "0.14"
loco-rs = "0.14"
tokio = { version = "1", features = ["full"] }
```

Django では `apig-wsgi` が WSGI アプリを Lambda ハンドラーに変換しますが、Loco（axum）では [`lambda_http`](https://crates.io/crates/lambda_http) が axum Router を直接 Lambda ハンドラーとして使えます。フレームワーク側で完結するため、Lambda Web Adapter のような外部 Extension は不要です。

### Lambda エントリポイント

`src/bin/lambda.rs` を作成します。

```rust
use loco_rs::boot::{create_app, StartMode};
use loco_rs::config::Config;
use loco_rs::environment::Environment;

use myapp::app::App;
use myapp::migration::Migrator;

#[tokio::main]
async fn main() -> Result<(), lambda_http::Error> {
    magic_pocket_rs::set_envs().await.unwrap();

    let environment: Environment = "production".parse().unwrap();
    let config = Config::new(&environment)?;
    let boot = create_app::<App, Migrator>(
        StartMode::ServerOnly,
        &environment,
        config,
    )
    .await?;

    lambda_http::run(boot.router.expect("no router")).await
}
```

### set_envs()

`set_envs()` は、Secrets Manager / SSM からシークレットを取得し、CloudFormation Output から API Gateway のホスト情報を取得して、すべて環境変数にセットします。

| 関数 | 説明 |
|------|------|
| `set_envs()` | シークレット + AWS リソース情報をすべてセット |
| `set_envs_from_secrets(stage)` | シークレットのみセット |
| `set_envs_from_resources(stage)` | AWS リソース情報のみセット |

`POCKET_STAGE` が設定されていない場合、シークレット取得はスキップされます（ローカル環境での安全動作）。

### セットされる環境変数

| 環境変数 | 説明 |
|----------|------|
| `POCKET_PROJECT_NAME` | プロジェクト名 |
| `POCKET_REGION` | AWS リージョン |
| `POCKET_HOSTS` | API Gateway ホスト一覧 |
| `POCKET_{HANDLER}_HOST` | 各ハンドラーのホスト |
| `POCKET_{HANDLER}_ENDPOINT` | 各ハンドラーの URL |
| `POCKET_{HANDLER}_QUEUEURL` | SQS キュー URL |
| シークレットキー | Secrets Manager / SSM のシークレット値 |

### pocket.toml の構成例（Loco）

```toml
[general]
region = "ap-northeast-1"
stages = ["dev", "prd"]

[s3]

[neon]
project_name = "dev-myproject"

[awscontainer]
dockerfile_path = "pocket.Dockerfile"

[awscontainer.handlers.wsgi]
command = "myapp-lambda"

[dev.awscontainer.handlers.wsgi]
apigateway = {}

[awscontainer.secrets.managed]
LOCO_SECRET_KEY = { type = "password", options = { length = 50 } }
DATABASE_URL = { type = "neon_database_url" }
```

### Dockerfile の例（Loco）

```dockerfile
FROM rust:1-bookworm AS builder
WORKDIR /app
COPY . .
RUN cargo build --release --bin myapp-lambda

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app/target/release/myapp-lambda /usr/local/bin/
COPY --from=builder /app/config /app/config
COPY --from=builder /app/pocket.toml /app/pocket.toml
WORKDIR /app
CMD ["myapp-lambda"]
```

`command` は Docker の CMD をオーバーライドするため、Dockerfile では `CMD` のみを使用し `ENTRYPOINT` は設定しないことを推奨します。詳細は「[設定ファイル - awscontainer.handlers](configuration.md#awscontainerhandlers)」を参照してください。

### Loco の注意点

#### production.yaml の環境変数クォート

magic-pocket が生成するシークレット（`type = "password"`）には `*`, `!`, `#` 等の YAML 特殊文字が含まれる場合があります。Loco の設定テンプレートでは環境変数を**必ずクォートで囲んで**ください。

```yaml
# NG - SECRET_KEY に * が含まれると YAML パースが壊れる
secret: {{ get_env(name="SECRET_KEY") }}

# OK
secret: "{{ get_env(name="SECRET_KEY") }}"
database:
  uri: "{{ get_env(name="DATABASE_URL") }}"
```

#### static ミドルウェアの無効化

Loco のデフォルト `production.yaml` には `static.must_exist: true` で `frontend/dist` を参照する設定があります。Lambda コンテナにフロントエンドを含めない場合は無効にしてください。

```yaml
static:
  enable: false
```

#### DB 接続タイムアウト

Neon がアプリケーションと異なるリージョンにある場合、デフォルトの接続タイムアウトでは不足する場合があります。クロスリージョン接続では余裕を持った値を設定してください。

```yaml
database:
  connect_timeout: 5000
  idle_timeout: 5000
```

!!! tip "Neon のリージョン選択"
    Neon プロジェクトは Lambda と同じリージョン（または近いリージョン）に作成することを推奨します。レイテンシと接続タイムアウトの問題を回避できます。

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
)

# AWS リソースから環境変数を登録（Lambda上のみ動作）
set_envs()

# 環境変数から読み込み
SECRET_KEY = env.str("SECRET_KEY")
DEBUG = env.bool("DEBUG")
DATABASES = {"default": env.db()}
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS")

# pocket.toml の django.settings を反映
vars().update(get_django_settings())
```
