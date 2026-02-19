# 設定ファイル (pocket.toml)

デプロイに関する全ての設定は `pocket.toml` に記述します。

## 基本構造

```toml
[general]           # 全ステージ共通の設定
[s3]                # S3設定（全ステージ共通）
[neon]              # Neon設定（全ステージ共通）
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
| `object_prefix` | str | `"pocket-"` | リソース名のプレフィックス |
| `project_name` | str | ディレクトリ名 | プロジェクト名（通常は自動取得） |
| `s3_fallback_bucket_name` | str \| None | None | ローカルでS3ストレージを使う場合のバケット名 |

### general.django_fallback

ローカル環境で使うDjango設定を記述します。設定項目は [`awscontainer.django`](#awscontainerdjango) と同じです。

```toml
[general.django_fallback.storages]
default = { store = "filesystem" }
staticfiles = { store = "filesystem", static = true }
```

### general.vpcs

VPC設定をリストで定義します。`awscontainer` から `vpc_ref` で参照します。

```toml
[[general.vpcs]]
ref = "main"
zone_suffixes = ["a"]
nat_gateway = true
internet_gateway = true

[general.vpcs.efs]
local_mount_path = "/mnt/efs"
access_point_path = "/lambda"
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `ref` | str | **必須** | 参照名 |
| `zone_suffixes` | list[str] | `["a"]` | AZサフィックス |
| `nat_gateway` | bool | `true` | NAT Gatewayを作成 |
| `internet_gateway` | bool | `true` | Internet Gatewayを作成 |
| `efs` | Efs \| None | None | EFS設定（下表参照） |

**EFS設定**

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `local_mount_path` | str | `"/mnt/efs"` | Lambda内のマウントパス（`/mnt/` で始まる必要あり） |
| `access_point_path` | str | `"/lambda"` | EFSアクセスポイントのパス |

---

## s3

S3バケットの設定です。

```toml
[s3]
public_dirs = ["static"]
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `public_dirs` | list[str] | `[]` | 公開するディレクトリ |
| `bucket_name_format` | str | `"{prefix}{stage}-{project}"` | バケット名のフォーマット |

`bucket_name_format` で使える変数:

- `{prefix}` — `object_prefix` の値
- `{stage}` — ステージ名
- `{project}` — プロジェクト名

??? example "prdのみバケットを分ける例"
    ```toml
    [s3]
    bucket_name_format = "{prefix}{project}"
    [prd.s3]
    bucket_name_format = "{prefix}{stage}-{project}"
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
| `vpc_ref` | str \| None | None | general.vpcsのrefを指定してVPCに接続 |

!!! info "VPCなしデプロイ"
    `vpc_ref` を省略するとLambdaはVPCの外（パブリック）で実行されます。
    VPC、NAT Gateway、EFSが不要な開発環境では、VPCなしの方がコスト効率が良く、コールドスタートも高速です。

!!! info "VPCと固定IP"
    `vpc_ref` を指定すると、Lambdaはプライベートサブネットに配置され、外部通信はNAT Gateway経由になります。
    `zone_suffixes` で定義したゾーンごとに1つのNAT Gateway（Elastic IP）が作成されるため、Lambdaの送信元IPはゾーンごとに固定されます。
    例えば `zone_suffixes = ["a"]`（デフォルト）なら固定IP 1つ、`zone_suffixes = ["a", "c"]` なら固定IP 2つです。

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
pocket_key_format = "{prefix}{stage}-{project}"
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `store` | `"sm"` \| `"ssm"` | `"sm"` | シークレットの保存先 |
| `pocket_key_format` | str | `"{prefix}{stage}-{project}"` | シークレットキーのフォーマット |
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
| `store` | `"s3"` \| `"cloudfront"` \| `"filesystem"` | **必須** | ストレージの種類 |
| `location` | str \| None | None | ファイル保存先（s3では必須、cloudfrontでは使用不可） |
| `static` | bool | `false` | StaticFileストレージを使用 |
| `manifest` | bool | `false` | ManifestStaticFilesStorageを使用（`static=true` 時のみ） |
| `options` | dict | `{}` | 追加オプション（Djangoの `STORAGES[key]["OPTIONS"]` にそのまま渡される） |

`store`, `static`, `manifest` の組み合わせで以下のバックエンドが選択されます。

| store | static | manifest | バックエンド |
|-------|--------|----------|------------|
| s3 | false | — | `storages.backends.s3boto3.S3Boto3Storage` |
| s3 | true | false | `storages.backends.s3boto3.S3StaticStorage` |
| s3 | true | true | `storages.backends.s3boto3.S3ManifestStaticStorage` |
| cloudfront | false | — | `pocket.django.storages.CloudFrontS3Boto3Storage` |
| cloudfront | true | false | `pocket.django.storages.CloudFrontS3StaticStorage` |
| cloudfront | true | true | `pocket.django.storages.CloudFrontS3ManifestStaticStorage` |
| filesystem | false | — | `django.core.files.storage.FileSystemStorage` |
| filesystem | true | false | `django.contrib.staticfiles.storage.StaticFilesStorage` |
| filesystem | true | true | `django.contrib.staticfiles.storage.ManifestStaticFilesStorage` |

!!! note "cloudfront ストレージについて"
    `store = "cloudfront"` を使う場合、`location` は使用できません。
    代わりに `options` に `cloudfront_ref` を指定して、CloudFrontのルートと紐付けます。

    ```toml
    [awscontainer.django.storages]
    staticfiles = { store = "cloudfront", static = true, manifest = true, options = { cloudfront_ref = "static" } }
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

CloudFrontディストリビューションとコンテンツ配信用S3バケットを作成します。
主にSPAのフロントエンド配信に使用します。

!!! info "この機能について"
    証明書、DNS設定、CloudFront設定、リダイレクト設定、コンテンツ保存用S3バケット作成を行います。
    SPAのビルドやバケットへのアップロードは別途必要です。

```toml
[dev.cloudfront]
domain = "dev.example.com"
routes = [{ is_spa = true }]

[prd.cloudfront]
domain = "www.example.com"
routes = [{ is_spa = true }]
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `domain` | str | **必須** | 配信ドメイン |
| `bucket_name_format` | str | `"{prefix}{project}-cloudfront"` | バケット名フォーマット |
| `origin_prefix_format` | str | `"/{stage}"` | オリジンパスのフォーマット |
| `hosted_zone_id_override` | str \| None | None | ホストゾーンIDを明示指定 |
| `redirect_from` | list[RedirectFrom] | `[]` | リダイレクト元ドメイン |
| `routes` | list[Route] | **必須** | ルーティング設定（最低1つ必要） |

!!! warning "フォーマットに関する制限"
    `bucket_name_format` と `origin_prefix_format` を合わせて、`{stage}` と `{project}` の両方が含まれている必要があります。

### redirect_from

```toml
[prd.cloudfront]
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
[prd.cloudfront]
domain = "www.example.com"
routes = [
    { is_spa = true },
    { path_pattern = "/assets/*", is_versioned = true },
]
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `path_pattern` | str | `""` | パスパターン（空文字列がデフォルトルート） |
| `is_spa` | bool | `false` | SPA用の設定（フォールバックHTML対応） |
| `is_versioned` | bool | `false` | バージョン付きアセット用（長期キャッシュ） |
| `spa_fallback_html` | str | `"index.html"` | SPAフォールバック先のHTML |
| `versioned_max_age` | int | `31536000` | バージョン付きアセットのmax-age（秒、デフォルト1年） |
| `ref` | str | `""` | ルートの参照名（cloudfront storageで使用） |

!!! note "制約"
    - `is_spa` と `is_versioned` は同時に `true` にできません。
    - `path_pattern` は空でないルートは `/` で始まる必要があります。
    - `routes` には `path_pattern` が空のデフォルトルートが1つ必要です。
