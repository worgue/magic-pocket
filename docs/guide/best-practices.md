# ベストプラクティス

Django API + SPA フロントエンドを同一ドメインで配信し、メディアファイルを署名付き URL で保護する推奨構成です。

## アーキテクチャ

```
Browser → CloudFront (example.com)
             ├─ /*         → Origin(path=/web/app) → S3: web/app/...
             │               CF Function: SPA fallback のみ
             ├─ /api/*     → API Gateway → Lambda (Django)
             └─ /static/*  → Origin(path=/web) → S3: web/static/... (deploystatic で管理)

Browser → CloudFront (media.example.com)
             └─ /*         → Origin(path=/usercontent) → S3: usercontent/... (署名付きURL)
```

- **SPA + API を同一ドメイン**に統合し、Cookie 認証（session + CSRF）をそのまま利用
- **メディアファイルは別ドメイン**で署名付き URL により保護
- **Django staticfiles は S3 に直接配置**し、Lambda コンテナには含めない

## 推奨 pocket.toml

```toml
[general]
region = "ap-northeast-1"
stages = ["dev", "prd"]

[general.django_fallback.storages]
default = { store = "filesystem" }
staticfiles = { store = "filesystem", static = true }

[s3]

[neon]
project_name = "dev-myproject"

[awscontainer]
dockerfile_path = "pocket.Dockerfile"

[awscontainer.handlers.wsgi]
command = "pocket.django.lambda_handlers.wsgi_handler"
apigateway = {}

[awscontainer.handlers.management]
command = "pocket.django.lambda_handlers.management_command_handler"
timeout = 600

[awscontainer.secrets.managed]
SECRET_KEY = { type = "password", options = { length = 50 } }
DJANGO_SUPERUSER_PASSWORD = { type = "password", options = { length = 16 } }
DATABASE_URL = { type = "neon_database_url" }
CF_MEDIA_KEY = { type = "cloudfront_signing_key", options = { pem_base64_environ_suffix = "_PEM_BASE64", pub_base64_environ_suffix = "_PUB_BASE64", id_environ_suffix = "_ID" } }

[awscontainer.django.storages]
default = { store = "s3", distribution = "usercontent" }
staticfiles = { store = "s3", distribution = "web", route = "static", static = true, manifest = true }

# SPA + API 同一ドメイン
[cloudfront.web]
routes = [
    { is_default = true, is_spa = true, build = "just frontend-build", build_dir = "frontend/dist", origin_path = "/web/app" },
    { path_pattern = "/static/*", ref = "static", is_versioned = true, origin_path = "/web" },
    { path_pattern = "/api/*", type = "api", handler = "wsgi" },
]

# メディア（署名付きURL）
[cloudfront.usercontent]
signing_key = "CF_MEDIA_KEY"
routes = [
    { is_default = true, signed = true, origin_path = "/usercontent" },
]

# --- ステージ固有設定 ---

[dev.awscontainer.handlers.wsgi]
apigateway = {}

[prd.neon]
project_name = "prd-myproject"

[prd.awscontainer.handlers.wsgi]
apigateway = {}

[prd.cloudfront.web]
domain = "example.com"
redirect_from = [{ domain = "www.example.com" }]

[prd.cloudfront.usercontent]
domain = "media.example.com"
```

## 設計の理由

**SPA + API 同一ドメイン（Cookie 認証）**
:   CloudFront が `/*` → S3、`/api/*` → API Gateway とルーティングします。
    同一ドメインなので Cookie がそのまま送信され、CORS 設定は不要です。
    Django 側では `CSRF_COOKIE_DOMAIN` と `CSRF_TRUSTED_ORIGINS` を設定してください。
    各 route の `origin_path` で S3 Origin の `OriginPath` を設定し、
    S3 上では `web/app/` 配下に SPA、`web/static/` 配下に staticfiles を分離配置します。

**`build` / `build_dir` でフロントエンド自動デプロイ**
:   `pocket deploy` でインフラ更新に加え、SPA のビルド → S3 アップロード → CloudFront キャッシュ無効化が自動実行されます。
    SPA の HTML は `no-cache`、アセットは `max-age=1年` のキャッシュヘッダーが設定されます。
    インフラのみ更新したい場合は `--skip-frontend` で抑制できます。

**staticfiles は `pocket django deploystatic` で管理**
:   Django の静的ファイル（admin CSS 等）はローカルで `collectstatic` → S3 アップロードする仕組みです。
    Lambda コンテナに静的ファイルを含めないため、イメージサイズを抑えられます。
    `distribution = "web"` と `route = "static"` を指定することで、CloudFront 経由（`example.com/static/...`）で配信されます。
    S3 上の location（`web/static`）は route の `origin_path` と `path_pattern` から自動計算されるため、`location` の手動指定は不要です。

**メディアを別 CloudFront + 署名付き URL で保護**
:   ユーザーがアップロードした画像・ファイル等は `media.example.com` 経由で配信します。
    `signing_key` による署名付き URL により、Django が生成した URL でのみアクセス可能です。
    鍵ペアの生成・管理は magic-pocket が自動で行います。

**Neon のプロジェクトをステージで分離**
:   dev と prd で Neon プロジェクトを分けることで、開発環境の操作が本番に影響しません。

??? tip "RDS Aurora を使う場合"
    Neon の代わりに RDS Aurora PostgreSQL Serverless v2 を使う場合は、`[neon]` と `DATABASE_URL` managed secret の代わりに `[rds]` を設定します。VPC 設定が必須です。

    ```toml
    [[general.vpcs]]
    ref = "main"
    zone_suffixes = ["a", "c"]

    [rds]
    vpc_ref = "main"

    [awscontainer]
    dockerfile_path = "pocket.Dockerfile"
    vpc_ref = "main"

    [awscontainer.secrets.managed]
    SECRET_KEY = { type = "password", options = { length = 50 } }
    # DATABASE_URL は不要。[rds] があれば自動提供
    ```

## SPA トークン認証

SPA にログイン必須機能を追加する場合、`require_token` を設定します。
CloudFront Function が Cookie 内の HMAC-SHA256 トークンを検証し、未認証ユーザーをログインページにリダイレクトします。

```toml
[awscontainer.secrets.managed]
SECRET_KEY = { type = "password", options = { length = 50 } }
DATABASE_URL = { type = "neon_database_url" }
SPA_TOKEN_SECRET = { type = "spa_token_secret" }

[cloudfront.web]
token_secret = "SPA_TOKEN_SECRET"
routes = [
    { is_default = true, is_spa = true, require_token = true, build = "just frontend-build", build_dir = "frontend/dist", origin_path = "/web/app" },
    { path_pattern = "/static/*", ref = "static", is_versioned = true, origin_path = "/web" },
    { path_pattern = "/api/*", type = "api", handler = "wsgi" },
]
```

**仕組み**

1. CloudFront Function（viewer-request）が全リクエストを検証
2. Cookie `pocket-spa-token` に含まれるトークン（`{user_id}:{expiry}:{hmac}`）を HMAC-SHA256 で検証
3. シークレットは CloudFront KeyValueStore (KVS) に格納（Function コードには埋め込まない）
4. 未認証・期限切れの場合、`login_path`（デフォルト `/api/auth/login`）に 302 リダイレクト
5. `/api/*` は API Gateway にルーティングされるため、トークン検証の対象外

**Django 側の実装**

```python
from django.http import HttpResponseRedirect
from pocket.django.spa_auth import spa_login, spa_logout

# ログインビュー（/api/auth/login）
def login_view(request):
    # Django 認証でユーザーを検証...
    response = HttpResponseRedirect(request.GET.get("next", "/"))
    spa_login(response, str(request.user.id))
    return response
```

詳細は「[実行環境とDjango連携 - SPA トークン認証](runtime.md#spa-トークン認証)」を参照してください。

## デプロイ手順

```bash
# 初回デプロイ
pocket deploy --stage=dev
pocket django manage migrate --stage=dev
pocket django deploystatic --stage=dev
pocket django manage createsuperuser --username=admin --email=admin@example.com --noinput --stage=dev

# 通常のデプロイ（インフラ + フロントエンド）
pocket deploy --stage=dev

# フロントエンドのみ更新
pocket resource cloudfront upload --stage=dev

# インフラのみ更新（フロントエンドスキップ）
pocket deploy --stage=dev --skip-frontend
```
