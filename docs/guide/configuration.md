# 設定ファイル (pocket.toml)

デプロイに関する全ての設定は `pocket.toml` に記述します。

## 基本構造

```toml
[general]           # 全ステージ共通の設定
[vpc]               # VPC設定（単一、トップレベル）
[s3]                # S3設定（全ステージ共通）
[neon]              # Neon設定（全ステージ共通）
[rds]               # RDS Aurora設定（全ステージ共通）
[awscontainer]      # Lambda設定（全ステージ共通）
[cloudfront]        # CloudFront設定（全ステージ共通）

[dev.awscontainer]  # dev ステージ固有のLambda設定
[prd.s3]            # prd ステージ固有のS3設定
```

!!! info "ステージ毎の設定"
    `[neon]` のようにステージ名なしで書くと、全ステージに適用されます。

    `[dev.neon]` のようにステージ名をプレフィックスにすると、そのステージのみに適用されます。
    ステージ固有の設定は、共通設定にマージされます。

---

## general（必須）

全ステージ共通の設定です。

```toml
[general]
region = "ap-northeast-1"
stages = ["dev", "prd"]
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `region` | str | **必須** | AWSリージョン |
| `stages` | list[str] | **必須** | ステージ名のリスト |
| `namespace` | str | `"pocket"` | リソース名の名前空間 |
| `project_name` | str | ディレクトリ名 | プロジェクト名（通常は自動取得） |
| `s3_fallback_bucket_name` | str \| None | None | ローカルでS3ストレージを使う場合のバケット名 |

### general.django_fallback

ローカル環境で使うDjango設定を記述します。設定項目は [`awscontainer.django`](#awscontainerdjango) と同じです。

```toml
[general.django_fallback.storages]
default = { store = "filesystem" }
staticfiles = { store = "filesystem", static = true }
```

---

## vpc

VPC設定をトップレベルで定義します。`[vpc]` を定義すると、`awscontainer` と `rds` は自動的に VPC 内に配置されます。

VPC名は `{ref}-{namespace}` 形式（例: `main-pocket`）になります。

```toml
[vpc]
ref = "main"
zone_suffixes = ["a", "c"]
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `ref` | str | **必須** | 参照名（VPC名の一部になる） |
| `zone_suffixes` | list[str] | `[]` | AZサフィックス（`manage=true` 時は必須） |
| `nat_gateway` | bool | `true` | NAT Gatewayを作成 |
| `internet_gateway` | bool | `true` | Internet Gatewayを作成 |
| `efs` | Efs \| None | None | EFS設定（下表参照、`manage=true` 時のみ） |
| `manage` | bool | `true` | VPCスタックを自分で管理する。`false` の場合は既存VPCを参照 |
| `sharable` | bool | `false` | 他プロジェクトからの共有を許可（`manage=true` 時のみ） |

**EFS設定**

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `local_mount_path` | str | `"/mnt/efs"` | Lambda内のマウントパス（`/mnt/` で始まる必要あり） |
| `access_point_path` | str | `"/lambda"` | EFSアクセスポイントのパス |

### VPC の共有（外部 VPC）

別プロジェクトが管理する VPC を利用する場合、`manage = false` を指定します。

**VPC 所有者（Project A）:**
```toml
[vpc]
ref = "main"
zone_suffixes = ["a", "c"]
sharable = true  # 共有を許可
```

**VPC 利用者（Project B）:**
```toml
[vpc]
ref = "main"        # 同じ ref → 同じ VPC
manage = false       # zone_suffixes 不要（自動取得）
```

!!! info "タグによる共有管理"
    VPC スタックの CloudFormation タグで共有状態を管理します。

    - `pocket:sharable = true` — 共有許可（所有者が設定）
    - `pocket:consumer:{slug} = deployed` — 利用者の登録（デプロイ時に自動追加、削除時に自動除去）

!!! warning "制約事項"
    - `manage=false` では `sharable`、`efs` は設定できません。
    - `manage=false` では `zone_suffixes` は不要です（VPC スタックから自動取得）。
    - consumer がいる VPC は削除できません。

### use_vpc（awscontainer / rds）

`awscontainer` や `rds` セクションで `use_vpc` を指定すると、VPC の利用を明示的に制御できます。

| 値 | 動作 |
|---|------|
| 未指定 | auto: `[vpc]` があれば VPC 内に配置 |
| `true` | 必須: `[vpc]` がなければエラー |
| `false` | VPC を使わない |

```toml
[vpc]
ref = "main"
zone_suffixes = ["a"]

[awscontainer]
dockerfile_path = "pocket.Dockerfile"
use_vpc = false  # VPC を使わない
```

---

## s3

S3バケットの設定です。

```toml
[s3]
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `bucket_name_format` | str | `"{stage}-{project}-{namespace}"` | バケット名のフォーマット |

`bucket_name_format` で使える変数:

- `{namespace}` — 名前空間
- `{stage}` — ステージ名
- `{project}` — プロジェクト名

??? example "prdのみバケットを分ける例"
    ```toml
    [s3]
    bucket_name_format = "{project}-{namespace}"
    [prd.s3]
    bucket_name_format = "{stage}-{project}-{namespace}"
    ```

---

## neon

Neon PostgreSQLの設定です。

```toml
[neon]
project_name = "dev-myproject"

[prd.neon]
project_name = "prd-myproject"
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `project_name` | str | **必須** | Neonプロジェクト名 |
| `pg_version` | int | `15` | PostgreSQLのバージョン |

`NEON_API_KEY` 環境変数（または `.env`）が必要です。ステージごとにNeonプロジェクトを分ける場合は、デプロイ時に環境変数を切り替えてください。

---

## rds

RDS Aurora PostgreSQL Serverless v2 の設定です。`[vpc]` と組み合わせてクラスターが自動作成されます。

```toml
[vpc]
ref = "main"
zone_suffixes = ["a", "c"]  # managed VPC では RDS に 2AZ 以上必須

[rds]

[awscontainer]
dockerfile_path = "pocket.Dockerfile"
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `min_capacity` | float | `0.5` | Serverless v2 最小キャパシティ（ACU） |
| `max_capacity` | float | `2.0` | Serverless v2 最大キャパシティ（ACU） |

!!! info "DATABASE_URL の自動構築"
    `[rds]` を設定すると、Lambda 環境変数 `POCKET_RDS_SECRET_ARN` が自動設定されます。
    `set_envs()` の呼び出し時に、RDS の AWS 管理シークレットから `DATABASE_URL` が自動構築されるため、
    `[awscontainer.secrets.managed]` に `DATABASE_URL` を定義する必要はありません。

!!! warning "制約事項"
    - managed VPC（`manage=true`）では `zone_suffixes` が 2 つ以上必要です（DB Subnet Group に最低 2AZ 必要）。
    - 外部 VPC（`manage=false`）ではサブネット数は自動検出されます。
    - `awscontainer` も同じ VPC に配置されている必要があります。
    - CloudFormation ではなく boto3 で直接管理されます（データ保持リソースのため）。

??? example "カスタムキャパシティの例"
    ```toml
    [rds]
    min_capacity = 1.0
    max_capacity = 8.0
    ```

---

## awscontainer

AWS Lambda コンテナの設定です。

```toml
[awscontainer]
dockerfile_path = "pocket.Dockerfile"
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `dockerfile_path` | str | **必須** | Dockerfileのパス |
| `platform` | str | `"linux/amd64"` | Dockerビルドプラットフォーム |
| `envs` | dict[str, str] | `{}` | Lambda環境変数 |
| `use_vpc` | bool \| None | None | VPC利用の制御（[use_vpc](#use_vpcawscontainer--rds) 参照） |

!!! info "VPCなしデプロイ"
    `[vpc]` セクションがない場合（または `use_vpc = false`）、LambdaはVPCの外（パブリック）で実行されます。
    VPC、NAT Gateway、EFSが不要な開発環境では、VPCなしの方がコスト効率が良く、コールドスタートも高速です。

!!! info "VPCと固定IP"
    `[vpc]` セクションがあると、Lambdaはプライベートサブネットに配置され、外部通信はNAT Gateway経由になります。
    `zone_suffixes` で定義したゾーンごとに1つのNAT Gateway（Elastic IP）が作成されるため、Lambdaの送信元IPはゾーンごとに固定されます。
    例えば `zone_suffixes = ["a"]` なら固定IP 1つ、`zone_suffixes = ["a", "c"]` なら固定IP 2つです。

### awscontainer.handlers

Lambda関数の設定を記述します。キー名がハンドラー名になります。

```toml
[awscontainer.handlers.wsgi]
command = "pocket.django.lambda_handlers.wsgi_handler"

[awscontainer.handlers.management]
command = "pocket.django.lambda_handlers.management_command_handler"
timeout = 600
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `command` | str | **必須** | Lambdaハンドラーのコマンド |
| `timeout` | int | `30` | タイムアウト（秒） |
| `memory_size` | int | `512` | メモリサイズ（MB） |
| `reserved_concurrency` | int \| None | None | 予約済み同時実行数 |

#### handlers.`name`.apigateway

API Gatewayの設定です。

```toml
# API Gatewayを有効にする（URLは自動生成）
[dev.awscontainer.handlers.wsgi]
apigateway = {}

# 独自ドメインを利用する場合
[prd.awscontainer.handlers.wsgi]
apigateway = { domain = "example.com" }
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `domain` | str \| None | None | カスタムドメイン |
| `create_records` | bool | `true` | Route53レコードを自動作成 |
| `hosted_zone_id_override` | str \| None | None | ホストゾーンIDを明示指定 |

#### handlers.`name`.sqs

SQSキューの設定です。マネジメントコマンドの非同期実行に使えます。

```toml
[awscontainer.handlers.sqsmanagement]
command = "pocket.django.lambda_handlers.management_command_handler"
timeout = 600
sqs = {}
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `batch_size` | int | `10` | バッチサイズ |
| `message_retention_period` | int | `345600` | メッセージ保持期間（秒） |
| `maximum_concurrency` | int | `2` | 最大同時実行数（最小2） |
| `dead_letter_max_receive_count` | int | `5` | DLQの最大受信回数 |
| `dead_letter_message_retention_period` | int | `1209600` | DLQメッセージ保持期間（秒） |
| `report_batch_item_failures` | bool | `true` | バッチアイテム失敗をレポート |

### awscontainer.secrets

シークレット管理の設定です。保存先として Secrets Manager (`sm`) と SSM Parameter Store (`ssm`) を選択できます。

```toml
[awscontainer.secrets]
store = "sm"  # "sm" (Secrets Manager) or "ssm" (SSM Parameter Store)
pocket_key_format = "{stage}-{project}-{namespace}"
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `store` | `"sm"` \| `"ssm"` | `"sm"` | シークレットの保存先 |
| `pocket_key_format` | str | `"{stage}-{project}-{namespace}"` | シークレットキーのフォーマット |
| `require_list_secrets` | bool | `false` | ListSecrets権限を付与 |

#### secrets.managed

magic-pocketが自動生成・管理するシークレットを定義します。

```toml
[awscontainer.secrets.managed]
SECRET_KEY = { type = "password", options = { length = 50 } }
DJANGO_SUPERUSER_PASSWORD = { type = "password", options = { length = 16 } }
DATABASE_URL = { type = "neon_database_url" }
```

**type = "password"**
:   パスワードを自動生成します。

    | オプション | 型 | デフォルト | 説明 |
    |-----------|------|----------|------|
    | `length` | int | `16` | パスワードの長さ |

**type = "neon_database_url"**
:   NeonのDB接続URLをAPI経由で取得し保存します。オプションはありません。

**type = "tidb_database_url"**
:   TiDB ServerlessのDB接続URLを取得し保存します。オプションはありません。

**type = "rsa_pem_base64"**
:   RSA鍵ペアを生成しbase64で保存します。環境変数はキー名+suffixで2つ登録されます。

    | オプション | 型 | 説明 |
    |-----------|------|------|
    | `pem_base64_environ_suffix` | str | 秘密鍵の環境変数名suffix |
    | `pub_base64_environ_suffix` | str | 公開鍵の環境変数名suffix |

    ```toml
    [awscontainer.secrets.managed.JWT_RSA]
    type = "rsa_pem_base64"
    options = { pem_base64_environ_suffix = "_PEM_BASE64", pub_base64_environ_suffix = "_PUB_BASE64" }
    ```
    → 環境変数 `JWT_RSA_PEM_BASE64` と `JWT_RSA_PUB_BASE64` が登録されます。

**type = "cloudfront_signing_key"**
:   CloudFront 署名付き URL 用の RSA 鍵ペアを生成しbase64で保存します。
    秘密鍵と公開鍵は Secrets Manager/SSM 経由で環境変数として登録されます。
    CloudFront PublicKey の ID は CloudFormation のクロススタック参照（`Fn::ImportValue`）で Lambda 環境変数に自動設定されるため、書き戻しは不要です。

    | オプション | 型 | 説明 |
    |-----------|------|------|
    | `pem_base64_environ_suffix` | str | 秘密鍵の環境変数名suffix |
    | `pub_base64_environ_suffix` | str | 公開鍵の環境変数名suffix |
    | `id_environ_suffix` | str | CloudFront PublicKey ID の環境変数名suffix |

    ```toml
    [awscontainer.secrets.managed.CF_MEDIA_KEY]
    type = "cloudfront_signing_key"
    options = { pem_base64_environ_suffix = "_PEM_BASE64", pub_base64_environ_suffix = "_PUB_BASE64", id_environ_suffix = "_ID" }
    ```
    → 環境変数 `CF_MEDIA_KEY_PEM_BASE64`, `CF_MEDIA_KEY_PUB_BASE64` が secrets 経由で、`CF_MEDIA_KEY_ID` が CloudFormation ImportValue 経由で登録されます。

**type = "spa_token_secret"**
:   SPA トークン認証用の HMAC-SHA256 シークレット（256-bit hex 文字列）を自動生成します。
    CloudFront の `token_secret` で参照し、`require_token` ルートのトークン検証に使用されます。
    オプションはありません。

    ```toml
    [awscontainer.secrets.managed]
    SPA_TOKEN_SECRET = { type = "spa_token_secret" }
    ```
    → 環境変数 `SPA_TOKEN_SECRET` が secrets 経由で登録されます。Django 側で `pocket.django.spa_auth` を使ってトークン生成・検証が可能です。

#### secrets.user

自分で作成したシークレットを参照する場合に使います。
指定すると、GetSecretValue / GetParameter 権限が自動付与されます。

```toml
[awscontainer.secrets.user]
MY_API_KEY = { name = "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:my-secret" }
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `name` | str | **必須** | シークレット名またはARN |
| `store` | `"sm"` \| `"ssm"` \| None | None | 保存先（Noneの場合 `secrets.store` を継承） |

#### secrets.extra_resources

追加のシークレットARN（正規表現可）に対してGetSecretValue / GetParameter 権限を付与します。

```toml
[awscontainer.secrets]
extra_resources = ["arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:my-prefix-*"]
```

### awscontainer.django

Lambda環境で利用するDjango設定を記述します。

#### storages

Djangoの `STORAGES` に設定する内容です。

```toml
[awscontainer.django.storages]
default = { store = "s3", location = "media" }
staticfiles = { store = "s3", location = "static", static = true, manifest = true }
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `store` | `"s3"` \| `"filesystem"` | **必須** | ストレージの種類 |
| `location` | str \| None | None | ファイル保存先（s3では必須） |
| `static` | bool | `false` | StaticFileストレージを使用 |
| `manifest` | bool | `false` | ManifestStaticFilesStorageを使用（`static=true` 時のみ） |
| `distribution` | str \| None | None | CloudFront distribution 名（`[cloudfront.xxx]` のキー名） |
| `route` | str \| None | None | CloudFront route の ref（省略時は自動解決） |
| `options` | dict | `{}` | 追加オプション（Djangoの `STORAGES[key]["OPTIONS"]` にそのまま渡される） |

`store`, `static`, `manifest`, `distribution` の組み合わせで以下のバックエンドが選択されます。

| store | distribution | static | manifest | バックエンド |
|-------|-------------|--------|----------|------------|
| s3 | なし | false | — | `storages.backends.s3boto3.S3Boto3Storage` |
| s3 | なし | true | false | `storages.backends.s3boto3.S3StaticStorage` |
| s3 | なし | true | true | `storages.backends.s3boto3.S3ManifestStaticStorage` |
| s3 | あり | false | — | `pocket.django.storages.CloudFrontS3Boto3Storage` |
| s3 | あり | true | false | `pocket.django.storages.CloudFrontS3StaticStorage` |
| s3 | あり | true | true | `pocket.django.storages.CloudFrontS3ManifestStaticStorage` |
| filesystem | — | false | — | `django.core.files.storage.FileSystemStorage` |
| filesystem | — | true | false | `django.contrib.staticfiles.storage.StaticFilesStorage` |
| filesystem | — | true | true | `django.contrib.staticfiles.storage.ManifestStaticFilesStorage` |

!!! note "CloudFront 経由の配信"
    `distribution` を指定すると、S3 に保存しつつ CloudFront 経由で配信します。
    `location` は `origin_path` からの相対パスになります。

    ```toml
    [awscontainer.django.storages]
    default = { store = "s3", location = "", distribution = "media" }
    staticfiles = { store = "s3", location = "static", static = true, manifest = true, distribution = "main" }
    ```

#### caches

Djangoの `CACHES` に設定する内容です。

```toml
[awscontainer.django.caches]
default = { store = "locmem" }
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `store` | `"locmem"` \| `"efs"` | **必須** | キャッシュの種類 |
| `location_subdir` | str | `"{stage}"` | EFS上のサブディレクトリ（`efs` の場合のみ） |

- `locmem` → `django.core.cache.backends.locmem.LocMemCache`
- `efs` → `django.core.cache.backends.filebased.FileBasedCache`（VPC + EFS設定が必要）

#### settings

環境毎にDjangoの任意のsettingsを設定できます。

```toml
[dev.awscontainer.django.settings]
DEFAULT_FROM_EMAIL = '"Dev" <test@example.com>'
CORS_ALLOWED_ORIGINS = ["https://dev.example.com"]

[prd.awscontainer.django.settings]
DEFAULT_FROM_EMAIL = '"Production" <noreply@example.com>'
CORS_ALLOWED_ORIGINS = ["https://www.example.com"]
```

`settings.py` での読み込み方法は「[実行環境とDjango連携](runtime.md#django-settings)」を参照してください。

---

## cloudfront

CloudFrontディストリビューションの設定です。名前付きサブテーブル `[cloudfront.xxx]` で複数のディストリビューションを定義できます。
S3バケットは `[s3]` で定義したものを共有し、`origin_path` でパスを分離します。

!!! info "この機能について"
    証明書、DNS設定、CloudFront設定、リダイレクト設定を行います。
    SPAのビルドやバケットへのアップロードは別途必要です。

```toml
[cloudfront.main]
domain = "www.example.com"
origin_path = "/spa"
routes = [
    { is_default = true, is_spa = true },
    { path_pattern = "/static/*", ref = "static", is_versioned = true },
]

[cloudfront.media]
domain = "media.example.com"
origin_path = "/media"
signing_key = "CF_MEDIA_KEY"
routes = [
    { is_default = true, signed = true },
]
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `domain` | str \| None | None | 配信ドメイン（省略時は `xxx.cloudfront.net`） |
| `origin_path` | str | `"/spa"` | S3 オリジンパス |
| `hosted_zone_id_override` | str \| None | None | ホストゾーンIDを明示指定 |
| `redirect_from` | list[RedirectFrom] | `[]` | リダイレクト元ドメイン |
| `routes` | list[Route] | **必須** | ルーティング設定（最低1つ必要） |
| `signing_key` | str \| None | None | 署名付きURL用のmanaged secret名 |
| `token_secret` | str \| None | None | SPA トークン認証用の managed secret 名（`type = "spa_token_secret"`） |

### redirect_from

```toml
[prd.cloudfront.main]
domain = "www.example.com"
redirect_from = [{ domain = "example.com" }]
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `domain` | str | **必須** | リダイレクト元ドメイン |
| `hosted_zone_id_override` | str \| None | None | ホストゾーンIDを明示指定 |

### routes

CloudFrontのキャッシュ動作ルーティングを定義します。

```toml
[prd.cloudfront.main]
domain = "www.example.com"
routes = [
    { is_default = true, is_spa = true },
    { path_pattern = "/assets/*", is_versioned = true },
]
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `type` | `"s3"` \| `"api"` | `"s3"` | ルートの種類 |
| `handler` | str \| None | None | API Gateway の handler 名（`type = "api"` 時必須） |
| `path_pattern` | str | `""` | パスパターン |
| `is_default` | bool | `false` | CloudFront の DefaultCacheBehavior として使用 |
| `is_spa` | bool | `false` | SPA用の設定（フォールバックHTML対応） |
| `is_versioned` | bool | `false` | バージョン付きアセット用（長期キャッシュ） |
| `spa_fallback_html` | str | `"index.html"` | SPAフォールバック先のHTML |
| `versioned_max_age` | int | `31536000` | バージョン付きアセットのmax-age（秒、デフォルト1年） |
| `ref` | str | `""` | ルートの参照名（Django storage の route で参照） |
| `signed` | bool | `false` | 署名付きURL（distribution に `signing_key` が必要） |
| `build` | str \| None | None | ビルドコマンド（省略時はビルドスキップ） |
| `build_dir` | str \| None | None | ビルド成果物ディレクトリ（設定時に自動アップロード対象） |
| `require_token` | bool | `false` | SPA トークン認証を有効化（`is_spa = true` 必須） |
| `login_path` | str | `"/api/auth/login"` | 未認証時のリダイレクト先パス |

!!! note "制約"
    - `routes` には `is_default = true` のルートが1つ必要です。
    - `is_default = true` のルートは `path_pattern` を空にする必要があります。
    - `is_spa` と `is_versioned` は同時に `true` にできません。
    - `path_pattern` は空でないルートは `/` で始まる必要があります。
    - `signed = true` のルートには、distribution に `signing_key` の設定が必要です。
    - `type = "api"` のルートでは `is_spa`, `is_versioned`, `signed`, `is_default`, `require_token`, `build`, `build_dir` は使用できません。
    - `handler` は `awscontainer.handlers` に定義されている必要があり、`apigateway` が設定されていなければなりません。
    - `build` を指定する場合は `build_dir` が必須です。
    - `require_token = true` のルートには `is_spa = true` が必須です。distribution に `token_secret` の設定が必要です。

### CloudFront 経由の API Gateway（Cookie 認証）

SPA と API を同一ドメインで配信し、Cookie（session + CSRF）認証を使う構成です。
`/api/*` → API Gateway、`/*` → S3（SPA）というルーティングを実現します。

```
Browser → CloudFront (example.com)
             ├─ /*       → S3 (SPA)
             └─ /api/*   → API Gateway → Lambda (Django)
```

```toml
[awscontainer.handlers.wsgi]
command = "pocket.django.lambda_handlers.wsgi_handler"
apigateway = {}

[cloudfront.main]
domain = "example.com"
routes = [
    { is_default = true, is_spa = true },
    { path_pattern = "/api/*", type = "api", handler = "wsgi" },
]
```

`type = "api"` のルートでは以下が自動設定されます:

- **CachePolicyId**: CachingDisabled（API レスポンスはキャッシュしない）
- **OriginRequestPolicy**: Cookie 全転送、allViewerExceptHostHeader、QueryString 全転送
- **AllowedMethods**: 全7メソッド（GET, HEAD, OPTIONS, PUT, PATCH, POST, DELETE）
- **Origin**: API Gateway（https-only、CloudFormation のクロススタック参照で接続）

!!! tip "Django CSRF の設定"
    CloudFront 経由の場合、`CSRF_COOKIE_DOMAIN` と `CSRF_TRUSTED_ORIGINS` を設定してください。

    ```python
    CSRF_COOKIE_DOMAIN = ".example.com"
    CSRF_TRUSTED_ORIGINS = ["https://example.com"]
    ```

!!! note "API Gateway のドメイン設定"
    `handler` の `apigateway` には独自ドメインを設定しないでください（`apigateway = {}` のみ）。
    CloudFront がフロントとなるため、API Gateway の execute-api エンドポイントがそのまま使われます。

### SPA トークン認証

SPA に HMAC-SHA256 トークンによるログイン必須機能を追加できます。
未認証ユーザーは CloudFront Function（viewer-request）でログインページにリダイレクトされます。
シークレットは CloudFront KeyValueStore (KVS) に格納され、Function コードには埋め込まれません。

```
未認証ユーザー → CloudFront
  → viewer-request: CloudFront Function (async)
    → SPA fallback（URI 書き換え）
    → KVS からシークレット取得
    → Cookie 'pocket-spa-token' の HMAC-SHA256 検証
    → 失敗 → 302 リダイレクト → login_path
  → 成功 → S3 オリジンへ
```

```toml
[awscontainer.secrets.managed]
SECRET_KEY = { type = "password", options = { length = 50 } }
SPA_TOKEN_SECRET = { type = "spa_token_secret" }

[cloudfront.main]
token_secret = "SPA_TOKEN_SECRET"
routes = [
    { is_default = true, is_spa = true, require_token = true, origin_path = "/app" },
    { path_pattern = "/api/*", type = "api", handler = "wsgi" },
]
```

トークンの形式は `{user_id}:{expiry_unix}:{hmac_hex}` です。
Django 側では `pocket.django.spa_auth` モジュールでトークンの生成・検証・Cookie 設定が可能です。
詳細は「[実行環境とDjango連携 - SPA トークン認証](runtime.md#spa-トークン認証)」を参照してください。

!!! warning "login_path の除外"
    `login_path`（デフォルト: `/api/auth/login`）はトークン検証の対象外にする必要があります。
    `type = "api"` ルートで `/api/*` を API Gateway にルーティングしている場合、ログインエンドポイントは API 側で処理されるためトークン検証は行われません。
