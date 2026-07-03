# 設定ファイル (pocket.toml)

デプロイに関する全ての設定は `pocket.toml` に記述します。

## 基本構造

```toml
[general]           # 全ステージ共通の設定
[vpc]               # VPC設定（単一、トップレベル）
[s3]                # S3設定（全ステージ共通）
[neon]              # Neon設定（全ステージ共通）
[tidb]              # TiDB Serverless設定（全ステージ共通）
[rds]               # RDS Aurora設定（全ステージ共通）
[ses]               # SES設定（全ステージ共通）
[awscontainer]      # Lambda設定（全ステージ共通）
[cloudfront]        # CloudFront設定（全ステージ共通）
[scheduler]         # EventBridge Scheduler 設定（全ステージ共通）

[dev.awscontainer]  # dev ステージ固有のLambda設定
[prod.s3]            # prod ステージ固有のS3設定
```

!!! info "ステージ毎の設定"
    `[neon]` のようにステージ名なしで書くと、全ステージに適用されます。

    `[dev.neon]` のようにステージ名をプレフィックスにすると、そのステージのみに適用されます。
    ステージ固有の設定は、共通設定にディープマージされます。`[general]` を含む全セクションが対象です。

---

## general（必須）

全ステージ共通の設定です。

```toml
[general]
region = "ap-northeast-1"
stages = ["dev", "prod"]
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `region` | str | **必須** | AWSリージョン |
| `stages` | list[str] | **必須** | ステージ名のリスト |
| `namespace` | str | `"pocket"` | リソース名の名前空間 |
| `project_name` | str | ディレクトリ名 | プロジェクト名（通常は自動取得） |
| `s3_fallback_bucket_name` | str \| None | None | ローカルでS3ストレージを使う場合のバケット名 |

??? example "ステージごとにリージョンを変える例"
    dev は Neon（シンガポール）に近い `ap-southeast-1`、prod は RDS（東京）の `ap-northeast-1` で運用する場合:

    ```toml
    [general]
    region = "ap-northeast-1"
    stages = ["dev", "prod"]

    [dev.general]
    region = "ap-southeast-1"
    ```

    `[dev.general]` の設定は `[general]` にマージされるため、`region` だけを上書きでき、他の設定（`stages`, `project_name` 等）はそのまま維持されます。

    !!! warning "リージョンを変えるとリソース名は同じでもリージョンが異なります"
        S3 バケット、CloudFormation スタック、ECR リポジトリ等はすべて `region` に基づいて作成されます。ステージ間でリージョンが異なる場合、同名のリソースが別リージョンに存在することになります。

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
| `cors` | S3Cors \| None | None | CORS 設定（下記参照） |
| `versioning` | bool | `false` | バケット versioning を有効化する（下記参照） |
| `lifecycle_rules` | list[S3LifecycleRule] | `[]` | Lifecycle ルール（下記参照） |

`bucket_name_format` で使える変数:

- `{namespace}` — 名前空間
- `{stage}` — ステージ名
- `{project}` — プロジェクト名

??? example "prodのみバケットを分ける例"
    ```toml
    [s3]
    bucket_name_format = "{project}-{namespace}"
    [prod.s3]
    bucket_name_format = "{stage}-{project}-{namespace}"
    ```

### cors

ブラウザから S3 presigned URL で直接アップロードする場合に必要な CORS 設定を宣言できます。

```toml
[s3]
cors = { methods = ["PUT", "GET"], cloudfront = "web" }
```

| フィールド | 型 | 説明 |
|-----------|------|------|
| `methods` | list[str] | 許可する HTTP メソッド（`"PUT"`, `"GET"` 等） |
| `cloudfront` | str \| list[str] | AllowedOrigins を解決する CloudFront ディストリビューション名 |

`cloudfront` で指定した `[cloudfront.xxx]` のドメインが AllowedOrigins に設定されます。

- カスタムドメインがある場合: `https://dev.example.com`
- カスタムドメインがない場合: `https://*.cloudfront.net`

`AllowedHeaders` は `["*"]`、`MaxAgeSeconds` は `3600` で固定です。

??? example "複数ディストリビューションの例"
    ```toml
    [s3]
    cors = { methods = ["PUT", "GET"], cloudfront = ["web", "media"] }
    ```

!!! note "宣言的に管理されます"
    `cors` を宣言しない場合、pocket は既存の bucket CORS 設定を**削除**します (`DeleteBucketCors`)。pocket 管理外で手動設定した CORS ルールも次回の `pocket resource s3 create` で削除されるため、手動ルールを残したい場合は toml に取り込んで宣言してください。

### versioning

`versioning = true` で S3 バケットの versioning を有効化します。`pocket resource s3 create` (再実行可能) で既存バケットにも冪等に適用されます。

```toml
[s3]
versioning = true
```

| 設定 | 動作 |
|------|------|
| `versioning = true` | `Enabled` に揃える (既に Enabled なら no-op) |
| `versioning = false` (デフォルト) | 現状が `Enabled` のときのみ `Suspended` に揃える。それ以外 (未設定 / `Suspended`) は no-op |

!!! warning "Suspended は完全な無効化ではない"
    S3 の仕様上、一度 `Enabled` にしたバケットは「未設定」状態には戻れず、`Suspended` までしか戻せません。`versioning = false` で `Suspended` に切り替えても、既に作成された旧バージョンオブジェクトは保持されます (lifecycle で消すか手動削除してください)。

!!! note "宣言的に管理されます"
    pocket は toml で宣言された `versioning` の値を bucket の真実 (source of truth) として扱います。pocket 管理外で手動変更した versioning 状態は、次回の `pocket resource s3 create` 実行時に toml の宣言で上書きされます。

### lifecycle_rules

Non-current version expiration を中心とした S3 Lifecycle ルールを宣言できます。`pocket resource s3 create` で冪等に reconcile されます。

```toml
[[s3.lifecycle_rules]]
id = "expire-non-current-static"
prefix = "static/"
noncurrent_version_expiration_days = 1

[[s3.lifecycle_rules]]
id = "expire-non-current-media"
prefix = "media/"
noncurrent_version_expiration_days = 1
```

| フィールド | 型 | 説明 |
|-----------|------|------|
| `id` | str | ルール ID（バケット内で一意） |
| `prefix` | str | 適用対象の prefix（`""` で全オブジェクト） |
| `noncurrent_version_expiration_days` | int (≥1) | 旧バージョンを期限切れにするまでの日数 |

| 設定 | 動作 |
|------|------|
| `[[s3.lifecycle_rules]]` を 1 件以上宣言 | 宣言したルール群でバケットの Lifecycle 設定を**置き換え** (`PutBucketLifecycleConfiguration`) |
| 宣言なし (デフォルト) | 既存の Lifecycle 設定を**削除** (`DeleteBucketLifecycle`) |

!!! note "宣言的に管理されます"
    pocket は toml で宣言された `lifecycle_rules` の内容を bucket の真実として扱います。pocket 管理外で手動追加した Lifecycle ルールは、次回の `pocket resource s3 create` 実行時に削除または上書きされます。手動ルールを残したい場合は toml に取り込んで宣言してください。

??? example "versioning + lifecycle の組み合わせ例"
    bucket-wide versioning を有効化しつつ、`static/` `media/` の旧バージョンは 1 日で期限切れにする例:

    ```toml
    [s3]
    versioning = true

    [[s3.lifecycle_rules]]
    id = "expire-non-current-static"
    prefix = "static/"
    noncurrent_version_expiration_days = 1

    [[s3.lifecycle_rules]]
    id = "expire-non-current-media"
    prefix = "media/"
    noncurrent_version_expiration_days = 1
    ```

    `projects/` 等の長期保管 prefix は lifecycle ルールを書かないことで版を残します。

---

## neon

Neon PostgreSQLの設定です。Neon プロジェクトは事前に [Neon Console](https://console.neon.tech/){:target="_blank"} で作成しておく必要があります。magic-pocket はプロジェクト内にブランチ・データベース・ロールを作成します。

```toml
[neon]
project_name = "dev-myproject"

[prod.neon]
project_name = "prod-myproject"
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `project_name` | str | **必須** | Neonプロジェクト名 |
| `pg_version` | int | `15` | PostgreSQLのバージョン |
| `provisioning` | `"deploy"` \| `"command"` | `"deploy"` | branch/role/db の provisioning を deploy が行うか、`pocket resource neon store-url` に委ねるか（下記参照） |

`NEON_API_KEY` 環境変数（または `.env`）が必要です。ステージごとにNeonプロジェクトを分ける場合は、デプロイ時に環境変数を切り替えてください。

!!! info "provisioning — デプロイロールから DB credentials を切り離す"
    既定 (`provisioning = "deploy"`) では、`pocket django deploy` がデプロイ中に Neon API を
    叩いてブランチ・データベース・ロール・エンドポイントを ensure し、`DATABASE_URL` を供給
    します（zero-config）。このため CI/CD のデプロイロールに Neon の API キーを渡す必要があり、
    「デプロイは AWS 操作のみ・DB レイヤの credentials は渡さない」という責務分離をしたい場合に
    支障になります。

    `provisioning = "command"` にすると、**deploy は Neon に一切触れません**（API call ゼロ /
    credential 不要）。provisioning は `pocket resource neon store-url` コマンドに分離し、deploy は
    事前に保存された `DATABASE_URL`（[stored mode](#awscontainersecretsuser) の user secret）
    を読むだけになります。

    ```toml
    [dev.neon]
    project_name = "dev-myproject"
    provisioning = "command"

    [dev.awscontainer.secrets.user]
    # store-url の保存先。pocket が正準名を導出する（stored mode）。
    DATABASE_URL = { type = "neon_database_url" }
    ```

    運用フロー（credential custody の分離）:

    | タイミング | 場所 | コマンド |
    |----------|------|---------|
    | 初回 / branch 切替時 | **Neon API キーを持つ host / 特権 CI** | `pocket resource neon store-url --stage=dev`（branch/role/db を ensure し `DATABASE_URL` を SSM/SM に保存） |
    | 通常デプロイ | CI/CD（**Neon credential 不要**） | `pocket django deploy --stage=dev` |
    | Neon リソース操作 | host | `pocket resource neon create / status / branch-out / ...` を引き続き利用 |

    `store-url` は Neon API キーを要する provisioning ステップなので、頻度が低く特権的な操作
    （host operator / 限定された CI ジョブ）に置き、credential を持たない通常デプロイと分離する
    のが推奨です。Neon の接続 URL は `reveal_password` 方式で**冪等**なため、`store-url` は何度
    実行しても同じ値を書きます。

!!! info "computed mode（非推奨）"
    従来の computed mode（`[awscontainer.secrets.managed]` に
    `DATABASE_URL = { type = "neon_database_url" }` を置き、deploy 時に URL を算出して
    pocket_store に保存）は **deprecated** です。deploy 時に warning を出します。
    `provisioning` + stored user secret（上記）へ移行してください。

!!! warning "Neon プロジェクトのリージョン"
    Neon プロジェクトは `[general].region` と同じリージョン（または近いリージョン）で作成してください。
    リージョンが異なると、Lambda ↔ Neon 間の通信がクロスリージョンとなりレイテンシが悪化します。

---

## tidb

TiDB Serverless（MySQL 互換）の設定です。

```toml
[tidb]
project = "1234567890123456789"

[prod.tidb]
project = "9876543210987654321"
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `project` | str | **必須** | TiDB Cloud のプロジェクト ID |
| `region` | str | `"ap-northeast-1"` | TiDB クラスターのリージョン |
| `provisioning` | `"deploy"` \| `"command"` | `"deploy"` | provisioning を deploy が行うか `pocket resource tidb store-url` に委ねるか（[neon](#neon) の同名フィールド参照） |

`TIDB_PUBLIC_KEY` と `TIDB_PRIVATE_KEY` 環境変数（または `.env`）が必要です。TiDB Cloud のコンソールから API キーを取得してください。

!!! note "クラスター名"
    クラスター名はプロジェクト名から自動生成されます（`{project_name}`）。

!!! warning "TiDB の store-url は password をローテーションする"
    `provisioning = "command"` で `pocket resource tidb store-url` を使う場合、TiDB Serverless には
    password の reveal API が無いため、**store-url は実行のたびに root password を再生成**します
    （Neon は冪等ですが TiDB は異なります）。既存 secret がある場合は誤実行防止のため `--force`
    が必要で、実行後は接続 URL が変わるため consumer の再デプロイが前提になります。

---

## upstash

Upstash Redis（サーバーレス Redis）の設定です。VPC 不要で Lambda から直接利用できます。

```toml
[upstash]

[awscontainer.secrets.managed]
REDIS_URL = { type = "upstash_redis_url" }

[awscontainer.django.caches]
default = { store = "redis" }
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `budget` | int | `20` | 月額上限（ドル）。最低値の $20 がデフォルト |
| `provisioning` | `"deploy"` \| `"command"` | `"deploy"` | provisioning を deploy が行うか `pocket resource upstash store-url` に委ねるか（[neon](#neon) の同名フィールド参照） |

!!! info "provisioning = command で credential なしデプロイ"
    `[upstash] provisioning = "command"` にすると deploy は Upstash に触れません。`REDIS_URL` を
    `[awscontainer.secrets.user]` に `{ type = "upstash_redis_url" }`（stored mode）で宣言し、
    deploy 前に `pocket resource upstash store-url --stage <stage>` で database を ensure して
    接続 URL を保存します。Upstash の URL は database の password 読み出しで**冪等**なため、
    `store-url` は何度実行しても同じ値を書きます。

`UPSTASH_EMAIL` と `UPSTASH_API_KEY` 環境変数（または `.env`）が必要です。Upstash Console の Account > Management API で API Key を取得してください。これらはデプロイ時のみ必要で、Lambda 実行時には不要です。

データベースは `{project_name}-{stage}` の名前で自動作成されます。プライマリリージョンは `ap-southeast-1`（シンガポール）に固定です。

!!! info "budget について"
    月額利用料が budget に達するとレート制限がかかり、コストは budget を超えません。Upstash の最低 budget は $20 です。利用が 70% と 90% に達した時点で通知が届きます。

!!! note "Django での利用"
    `store = "redis"` を指定すると `django-redis` バックエンドが使用されます。`django-redis` のインストールが必要です。`REDIS_URL` は managed secrets から自動設定されます。

---

## dsql

Amazon Aurora DSQL の設定です。`[dsql]` を追加するだけでクラスターが自動作成されます。VPC は不要です。

```toml
[dsql]
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `deletion_protection` | bool | `false` | 削除保護の有効化 |

Lambda 環境変数として `POCKET_DSQL_ENDPOINT` と `POCKET_DSQL_REGION` が自動設定されます。
`set_envs()` の呼び出し時に、IAM 認証トークンが `POCKET_DSQL_TOKEN` に設定されます。

!!! warning "互換性に関する注意"
    DSQL は PostgreSQL 互換ですが、完全な互換ではありません。
    Django ORM のマイグレーション、contrib（auth, admin 等）、およびほとんどの 3rd パーティライブラリは正常に動作しません。
    Loco も同様です。
    アプリケーションが DSQL の制約を理解した上で、直接 SQL を実行する用途に適しています。

!!! info "認証方式"
    DSQL はパスワード認証ではなく IAM 認証トークンを使用します。
    `POCKET_DSQL_TOKEN` は `set_envs()` 呼び出し時（Lambda の cold start）に **1 回だけ** 生成され、トークンは約 **15 分** で失効します。

!!! warning "warm Lambda での再接続に注意（トークン期限切れ）"
    `POCKET_DSQL_TOKEN` は cold start で固定されるため、15 分以上稼働した warm Lambda が
    **新しい接続を張る**と、期限切れトークンで認証に失敗します（既存の確立済み接続は
    PostgreSQL の仕様上そのまま使えます）。新規接続の直前に `pocket.runtime.refresh_dsql_token()`
    を呼んでトークンを再生成してください。

    ```python
    from pocket.runtime import refresh_dsql_token

    token = refresh_dsql_token()  # POCKET_DSQL_TOKEN を最新化し、最新トークンを返す
    conn = psycopg.connect(
        host=os.environ["POCKET_DSQL_ENDPOINT"],
        user="admin",
        password=token,
        dbname="postgres",
        sslmode="require",
    )
    ```

    Django の `CONN_MAX_AGE`（接続の再利用時間）を 15 分より十分短くしておくと、期限切れ
    トークンを掴んだ接続が再利用される時間を抑えられます。

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
| `managed` | bool | `true` | `true` = pocket がクラスタを作成・管理、`false` = 既存クラスタを参照 |
| `min_capacity` | float | `0.5` | Serverless v2 最小キャパシティ（ACU）。`managed = true` のみ |
| `max_capacity` | float | `2.0` | Serverless v2 最大キャパシティ（ACU）。`managed = true` のみ |
| `snapshot_identifier` | str \| None | None | 初回作成時に復元する snapshot の ID / ARN。`managed = true` のみ |
| `database` | str \| None | None | DB 名の上書き。未指定なら `{stage}_{project}`（他リソース名と同じ順序）。`managed = true` のみ |
| `secret_arn` | str \| None | None | 既存 RDS の Secrets Manager ARN。`managed = false` 時必須 |
| `security_group_id` | str \| None | None | 既存 RDS の SG ID。`managed = false` 時必須 |

!!! info "DATABASE_URL の設定"
    `[awscontainer.secrets.managed]` に `DATABASE_URL = { type = "rds_database_url" }` または `{ type = "auto_database_url" }` を定義してください。
    Lambda の cold start 時に `POCKET_RDS_SECRET_ARN` から DATABASE_URL が動的に構築されます。

!!! info "master password 自動ローテーションへの追従"
    RDS は `ManageMasterUserPassword=True` で作成され、master password は AWS により自動ローテーション（デフォルト 7 日周期）されます。`get_databases()` は `[rds]` 設定時に RDS 専用の DB backend (`pocket.django.db_backends.rds`) を自動選択し、**接続確立時に認証エラー（PostgreSQL SQLSTATE class 28）を検知すると Secrets Manager から最新パスワードを取り直して 1 度だけ自動再接続します**。

    これにより、ローテーション直後に warm Lambda が古いパスワードで失敗し続けることはなく、cold start を待たずに自己修復します（手動介入不要）。既に確立済みの接続は PostgreSQL の仕様上ローテーション後も生き続けるため、影響を受けるのは再接続が必要になった瞬間だけです。

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

### snapshot からの復元

既存の RDS / Aurora データを新クラスタに持ち込みたい場合、`snapshot_identifier` を指定すると **初回作成時のみ** snapshot から復元されます。awsde などの他ツールからの本番移行、手動バックアップからの起動に利用できます。

```toml
[prod.rds]
snapshot_identifier = "myapp-prod-migration-20260410"
```

#### ID でも ARN でも指定可能

`snapshot_identifier` は **1 フィールドで ID / ARN 両対応** です。AWS の `RestoreDBClusterFromSnapshot` API が同じパラメータに ID・ARN どちらも受け付けるため、用途に応じて書き分けてください。

| 用途 | 書き方 | 例 |
|---|---|---|
| 同一アカウントの snapshot | **ID** | `"myapp-prod-20260410"` |
| 別アカウントの snapshot（クロスアカウント） | **ARN** | `"arn:aws:rds:ap-northeast-1:123456789012:cluster-snapshot:myapp-prod-20260410"` |
| 自動バックアップ snapshot | **ARN** | `"arn:aws:rds:ap-northeast-1:123456789012:cluster-snapshot:rds:myapp-prod-2026-04-10-03-07"` |

#### 初回作成のみ / 2 回目以降は無視される

!!! tip "復元が終わったら pocket.toml から消して良い"
    `snapshot_identifier` は **クラスタがまだ存在しないときだけ** 読まれます。

    - 1 回目の `pocket deploy` で snapshot から復元 → クラスタ作成
    - 2 回目以降の `pocket deploy` では `snapshot_identifier` の値は **一切読まれません**
    - そのため、復元が完了したら `pocket.toml` から `snapshot_identifier` を **削除して OK** です（残しておいても害はありません）
    - 復元済みクラスタに別の snapshot ID を書いても **クラスタは再作成されません**（安全側の挙動で drift も起きません）

    これが可能なのは、pocket が RDS を CloudFormation ではなく boto3 で直接管理しているためです。CloudFormation ベースのツールでは `SnapshotIdentifier` を後から消すとリソース置換が起きる（＝本番クラスタが吹き飛ぶ）典型的な罠がありますが、pocket ではその心配はありません。

#### マスターパスワードの扱い

snapshot から復元すると Aurora のマスターパスワードは snapshot 内のものが引き継がれます。pocket はこれを検出し、**復元直後に自動で AWS 管理シークレットに切り替え**ます:

1. `RestoreDBClusterFromSnapshot` で復元
2. クラスタ available まで待機
3. `ModifyDBCluster(ManageMasterUserPassword=True, ApplyImmediately=True)` を実行
4. 再度 available まで待機

この結果、`DATABASE_URL` は引き続き Secrets Manager から動的に構築され、パスワードローテーションも有効になります。ユーザー側で追加の手順は不要です。

#### 復元で注意すべきこと

!!! warning "バージョン互換性"
    復元されたクラスタの Aurora / Postgres バージョンは **snapshot 側のもの**です。古いバージョンから復元した場合、そのまま運用するかバージョンアップするかは別途判断してください。バージョンアップする場合は復元完了後に `aws rds modify-db-cluster --engine-version ...` を手動で実行します。

!!! warning "本番移行は必ず staging で先に試す"
    本番の snapshot を使う前に、必ず staging 環境で以下の流れを通してください:

    1. staging 用の snapshot を取得
    2. `[stg.rds]` に `snapshot_identifier` を設定して deploy
    3. クラスタ起動、マスターパスワード切替、DATABASE_URL 動作、アプリから DB アクセスまでの一連動作を確認
    4. 問題なければ本番 snapshot で prod を deploy

    Postgres バージョン互換性、VPC/SG 疎通、Secrets Manager 切替など、実環境で初めて顕在化する問題が複数あります。

!!! info "VPC / Subnet Group は別物で OK"
    snapshot の元クラスタと、pocket が作る新クラスタの VPC / Subnet Group は **別物で構いません**。`[vpc]` で指定した pocket 管理の VPC にそのまま復元されます。

!!! warning "復元クラスタの DB 名は snapshot 側のまま（`database` で追従）"
    `RestoreDBClusterFromSnapshot` は `DatabaseName` を**無視**するため、復元後のクラスタには **snapshot 元の DB 名**がそのまま残ります。pocket の既定 DB 名は `{stage}_{project}` なので、元ツールが別の命名（例 `{project}_{stage}`）だった場合、pocket は存在しない DB に接続しようとして `FATAL: database "..." does not exist` になります。

    復元元の実 DB 名に合わせるには `database` で上書きしてください:

    ```toml
    [prod.rds]
    snapshot_identifier = "myapp-prod-migration-20260410"
    database = "prod_myapp"  # 復元元 (snapshot) の実 DB 名に合わせる
    ```

    あるいは復元後に `ALTER DATABASE <old> RENAME TO <new>` で pocket 既定名へ寄せても構いません。

### 既存 RDS への接続 (`managed = false`)

pocket が作成・管理しない既存の RDS クラスタに Lambda から接続する場合、`managed = false` を指定します。pocket はクラスタの作成・削除を行わず、IAM と SG ingress のみを設定します。

```toml
[rds]
managed = false
secret_arn = "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:my-db-secret"
security_group_id = "sg-0123456789abcdef0"
```

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `managed` | bool | - | `false` で既存参照モード。デフォルト `true` |
| `secret_arn` | str | `managed=false` 時必須 | RDS の Secrets Manager シークレット ARN。host/port/username/password/dbname を含むこと |
| `security_group_id` | str | `managed=false` 時必須 | RDS のセキュリティグループ ID。Lambda SG → この SG への ingress が追加される |

!!! info "DATABASE_URL の構築"
    `managed = false` でも `managed = true` と同じく、Lambda 起動時に `POCKET_RDS_SECRET_ARN` から `DATABASE_URL` が動的に構築されます。`[awscontainer.secrets.managed]` に `DATABASE_URL` を定義する必要はありません（pocket が自動で注入します）。

!!! warning "制約"
    - `managed = false` では `min_capacity`, `max_capacity`, `snapshot_identifier` は使用できません
    - `secret_arn` と `security_group_id` は `managed = false` でのみ使用可能です（`managed = true` で指定するとエラー）
    - VPC 設定 (`[vpc]`) は不要です（Lambda と RDS が同一 VPC にいる前提で、SG ingress のみで接続します）

---

## ses

Amazon SES によるメール送信の設定です。設定すると、Lambda に `AmazonSESFullAccess` IAM ポリシーが付与されます。

```toml
[ses]
from_email = "noreply@example.com"
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `from_email` | str | **必須** | デフォルトの送信元メールアドレス |
| `region` | str \| None | None | SES リージョン（省略時は `general.region` を継承） |
| `configuration_set` | str \| None | None | SES Configuration Set 名 |

??? example "リージョンを指定する例"
    ```toml
    [ses]
    from_email = "noreply@example.com"
    region = "us-east-1"
    configuration_set = "my-tracking-set"
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
| `ecr_name` | str \| None | None | ECRリポジトリ名の上書き。省略時は `{stage}-{project}-{namespace}-lambda` |
| `build` | str \| dict | `"codebuild"` | コンテナイメージのビルドバックエンド（下記参照） |
| `permissions_boundary` | str \| None | None | Lambda 実行ロール / CodeBuild ロールに適用する IAM Permissions Boundary の ARN（[IAM 権限](../permissions/aws.md) 参照） |

### build（ビルドバックエンド）

コンテナイメージのビルド方法を指定します。文字列で backend のみ指定するショートハンドと、テーブルでの詳細指定の両方が使えます。

```toml
[awscontainer]
build = "docker"   # ショートハンド

# または詳細指定
[awscontainer.build]
backend = "codebuild"
compute_type = "BUILD_GENERAL1_MEDIUM"
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `backend` | `"codebuild"` \| `"docker"` \| `"depot"` | `"codebuild"` | ビルドバックエンド。`codebuild` = AWS CodeBuild 上でビルド（ローカル Docker 不要）、`docker` = ローカルの Docker でビルド、`depot` = [Depot](https://depot.dev/) でビルド |
| `compute_type` | str | `"BUILD_GENERAL1_MEDIUM"` | CodeBuild のコンピュートタイプ（`backend = "codebuild"` 時のみ） |
| `depot_project_id` | str \| None | None | Depot のプロジェクトID（`backend = "depot"` 時に必要） |

!!! info "`ecr_name` とステージ間のリポジトリ共有"
    デフォルトの ECR リポジトリ名にはステージ名が含まれるため、ステージごとに別リポジトリになります。
    同一 AWS アカウント内の複数ステージで同じ `ecr_name` を指定するとリポジトリを共有でき、
    [build once の昇格](cli.md#build-once)（`pocket django build` + `promote`）がタグの付け替えだけで成立します。

    `ecr_name` を明示指定したリポジトリは、他ステージと共有されている可能性があるため
    `pocket destroy` では削除されません（警告を表示してスキップします）。不要になった場合は手動で削除してください。

!!! info "Docker ビルドコンテキスト"
    Docker ビルドコンテキストは **pocket.toml のあるディレクトリ**（= `pocket deploy` を実行するディレクトリ）です。
    `dockerfile_path` はそこからの相対パスで指定します。

    uv workspace でフロントエンドとバックエンドを分けている場合、pocket.toml を**プロジェクトルート**に配置すると、
    ルートの `uv.lock` を Dockerfile 内で直接参照できます。

    ```toml
    # プロジェクトルートの pocket.toml
    [awscontainer]
    dockerfile_path = "backend/pocket.Dockerfile"
    ```

    ```dockerfile
    # ビルドコンテキスト = プロジェクトルート
    COPY uv.lock pyproject.toml backend/pyproject.toml ./
    RUN uv sync --frozen --no-dev --no-install-project --package my-backend

    COPY backend/src/ .
    ```

    Lambda 上でランタイム設定が必要です。
    `pocket deploy` 時にビルド専用設定を除外した `pocket.runtime.toml` が自動生成され、
    Docker ビルドコンテキストに配置されます。
    Dockerfile で `COPY pocket.runtime.toml ./` としてコピーしてください。

### pocket runtime-config

`pocket.toml` からビルド専用の設定（`dockerfile_path`, `managed_assets`, `build_dir` 等）を除外した TOML を生成します。

```bash
# 標準出力に出力
pocket runtime-config

# ファイルに出力
pocket runtime-config pocket.runtime.toml
```

`pocket deploy` 時にはビルド前に自動生成され、Lambda image に `COPY` されます。
手動実行は生成内容の確認やデバッグ用途で使えます。

Lambda 上では `pocket.runtime.toml` が `pocket.toml` より優先して読み込まれます。

!!! warning "生成物は `.gitignore` 推奨"
    `pocket deploy` (および `pocket django deploy`) は以下のファイルを再生成
    します。**いずれも `pocket.toml` から都度組み立て直す副産物なので、git
    管理は不要**です。誤コミットを防ぐため `.gitignore` に登録しておいて
    ください。

    | パス | 内容 |
    |------|------|
    | `pocket.runtime.toml` | `pocket.toml` の runtime 用 sanitized 版。`awscontainer.django.project_dir` が設定されていれば `{project_dir}/pocket.runtime.toml` に出力 |
    | `pocket_cache/` | `pocket django deploystatic` の中間ビルド成果物 (`static_build/<stage>/`)。S3 アップロード後は不要 |

    `.gitignore` の例:

    ```gitignore
    # magic-pocket: deploy のたび再生成される副産物 (git 管理不要)
    /pocket.runtime.toml
    /src/pocket.runtime.toml   # project_dir = "src" の場合
    /pocket_cache/
    ```

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
| `command` | str | **必須** | Lambda コンテナの `ImageConfig.Command`（エントリーポイント） |
| `timeout` | int | `30` | タイムアウト（秒） |
| `memory_size` | int | `512` | メモリサイズ（MB） |
| `reserved_concurrency` | int \| None | None | 予約済み同時実行数 |

`command` は Lambda コンテナイメージの CMD を上書きする値です（CloudFormation の `ImageConfig.Command` にマップされます）。ENTRYPOINT はオーバーライドしません。

- **Django**: Python モジュールパス形式のハンドラー関数を指定します（例: `pocket.django.lambda_handlers.wsgi_handler`）
- **Rust**: コンテナ内のバイナリパスを指定します（例: `myapp-lambda`）

!!! warning "ENTRYPOINT と CMD の関係"
    `command` は Docker の CMD のみをオーバーライドします。

    - Dockerfile が `CMD ["binary"]` の場合 → `command = "binary"` でそのまま起動されます（**推奨**）
    - Dockerfile が `ENTRYPOINT ["binary"]` + `CMD ["arg"]` の場合 → `command = "arg"` とすると `binary arg` で起動されます

    意図しない起動を避けるため、Dockerfile では `ENTRYPOINT` を使わず `CMD` のみで指定することを推奨します。

#### handlers.`name`.apigateway

API Gatewayの設定です。

```toml
# API Gatewayを有効にする（URLは自動生成）
[dev.awscontainer.handlers.wsgi]
apigateway = {}

# 独自ドメインを利用する場合
[prod.awscontainer.handlers.wsgi]
apigateway = { domain = "example.com" }
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `domain` | str \| None | None | カスタムドメイン |
| `create_records` | bool | `true` | Route53レコードを自動作成 |
| `hosted_zone_id_override` | str \| None | None | ホストゾーンIDを明示指定 |

!!! warning "create_records=False 利用時の orphan に注意"
    `create_records = false` を指定すると、Route53 の A レコードに加えて
    **ACM 証明書の検証用 CNAME も pocket の CloudFormation 管理外** になります。
    スタック削除時にこれらが orphan として残るため、必要に応じて手動削除してください。

    - ACM 証明書 (region: API Gateway と同じ): スタック削除後 `InUse: false` で残存（課金なし）
    - 検証用 CNAME (`_<hash>.<domain>.` → `*.acm-validations.aws.`): Route53 に残存

    デフォルト (`create_records = true`) では検証 CNAME も pocket 管理になるため、
    スタック削除時に自動で消えます。

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
DATABASE_URL = { type = "auto_database_url" }
```

**type = "auto_database_url"**
:   pocket.toml 内の DB 設定（`[neon]` / `[rds]` / `[tidb]`）を自動検出し、適切な DATABASE_URL を生成します。
    DB が1つだけ定義されている場合はそれを使用し、複数定義されている場合はエラーになります。
    ステージごとに DB を切り替える場合に便利です。オプションはありません。

    ```toml
    [awscontainer.secrets.managed]
    DATABASE_URL = { type = "auto_database_url" }

    [dev.neon]
    project_name = "dev-myproject"

    [prod.rds]
    ```

**type = "password"**
:   パスワードを自動生成します。

    | オプション | 型 | デフォルト | 説明 |
    |-----------|------|----------|------|
    | `length` | int | `16` | パスワードの長さ |

**type = "neon_database_url"**
:   NeonのDB接続URLをAPI経由で取得し保存します（computed）。オプションはありません。
    deploy 環境に管理 API key を置きたくない場合は、同じ type を `secrets.user` に置く
    stored mode も使えます（[DB 接続 URL の computed / stored](#db-接続-url-の-computed-mode-と-stored-mode) 参照）。

**type = "tidb_database_url"**
:   TiDB ServerlessのDB接続URLを取得し保存します（computed）。オプションはありません。
    stored mode も利用可（[同上](#db-接続-url-の-computed-mode-と-stored-mode)）。

**type = "rds_database_url"**
:   RDS Aurora の DATABASE_URL を設定します。実際の URL は Lambda 起動時に `POCKET_RDS_SECRET_ARN` から動的に構築されます（パスワードローテーション対応）。オプションはありません。

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
| `name` | str \| None | None | シークレット名またはARN。`type` と排他 |
| `type` | `"neon_database_url"` \| `"tidb_database_url"` \| None | None | DB URL の stored mode（後述）。`name` と排他 |
| `store` | `"sm"` \| `"ssm"` \| None | None | 保存先（Noneの場合 `secrets.store` を継承） |

`name` と `type` はどちらか一方を指定します（両方／どちらも無しはエラー）。

`name` には `{stage}` / `{project}` / `{namespace}` の format 変数が使えます
（`bucket_name_format` / `pocket_key_format` と同じ仕組み）。ステージ単位で
SSM パスや SM シークレットを分けたい場合に便利です。

```toml
[awscontainer.secrets.user]
# prod stage の Lambda は /svc/prod-token を、dev stage は /svc/dev-token を読む
SERVICE_TOKEN = { name = "/svc/{stage}-token", store = "ssm" }
```

##### DB 接続 URL の computed mode と stored mode

DB の接続 URL (`DATABASE_URL`) は 2 通りの解決方法があります。

| | computed（`secrets.managed`）**※非推奨** | stored（`secrets.user`） |
|---|---|---|
| 書き方 | `DATABASE_URL = { type = "tidb_database_url" }` を **managed** に | 同じ `type` を **user** に |
| URL を作るのは | pocket が deploy 時に provider の管理 API を叩いて計算（cluster lookup / password reset） | 事前 provision して secret store に保存（`pocket <db> store-url` または手動） |
| deploy 環境に管理 API key | **必要** | **不要** |
| pocket が値を生成 | する（pocket_store に保存） | しない（既存値を参照するだけ） |

!!! warning "computed mode は非推奨"
    computed mode（`secrets.managed` に DB URL の `type`）は **deprecated** です。deploy 時に
    warning を出します。stored mode + `[<db>] provisioning`（[neon](#neon) 参照）へ移行して
    ください。

stored mode は「provider の管理 API key を deploy 環境に置きたくない」「provisioning と
deploy を分離したい」「deploy を外部 API に依存させたくない（CI など）」場合に使います。

```toml
[neon]
project_name = "dev-myproject"
provisioning = "command"   # deploy は Neon に触れない

[awscontainer.secrets.user]
# 事前に provision した接続 URL を参照するだけ（pocket は deploy 時に API を叩かない）
DATABASE_URL = { type = "neon_database_url" }
```

secret の provision は `pocket resource neon store-url --stage <stage>` / `pocket resource tidb store-url
--stage <stage>` が便利です（branch/cluster/role/db を ensure し、接続 URL を上記 user
secret の正準名へ保存）。`[<db>] provisioning = "command"` と組み合わせると、provisioning
（管理 API key 必要）と deploy（credential 不要）を分離できます。手動で正準名に値を投入
しても構いません。

- 対象 `type` は `neon_database_url` / `tidb_database_url` の 2 つ。これらは computed だと
  deploy 時に管理 API key を要求するため、stored 化の利点が大きい type です。
- `rds_database_url` は user 側で使えません。RDS は元々 deploy 時に管理 API key を要求せず、
  接続 URL を Lambda 起動時に `POCKET_RDS_SECRET_ARN` から動的構築してパスワード
  ローテーションに追従します。静的な stored URL にするとローテで失効するため対象外です。
- **secret は deploy 前に provision しておく必要があります。** `type` 指定時、pocket は
  secret 名を自動導出します（managed の pocket_store パスとは衝突しない別名）。未 provision
  のまま deploy すると、pocket が期待する正準名を示して **deploy 時にエラー**で止まります
  （runtime まで遅延しません）。エラーメッセージに出る名前にその store（sm/ssm）で値を
  投入してください。値は接続 URL 文字列です。

#### secrets.extra_resources

追加のシークレットARN（正規表現可）に対してGetSecretValue / GetParameter 権限を付与します。

```toml
[awscontainer.secrets]
extra_resources = ["arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:my-prefix-*"]
```

#### secrets の即時反映 (`pocket resource awscontainer reload-env`)

SSM / Secrets Manager 側でシークレット値を更新しても、**warm container は
旧値を抱えたまま再利用される**ため、新値の反映は次の cold start を待つ
形になります (典型的には 5〜15 分のラグ)。feature flag の即時切替、secret
rotation 後の即時反映、hotfix で env 1 つだけ変えたい等のユースケースで
このラグが許容できない場合は、`pocket resource awscontainer reload-env` を使います。

```bash
# 全 handler の env を SSM/SM の最新値で再構築 + 即時反映
pocket resource awscontainer reload-env --stage=prod

# 特定 handler のみ
pocket resource awscontainer reload-env --stage=prod --handler=wsgi

# 現状確認 (Lambda 側の env と SSM/SM の宣言値が drift してないか)
pocket resource awscontainer status-env --stage=prod
```

仕組み:

1. pocket.toml の `[awscontainer.secrets.managed/user]` から「現在の宣言上の
   secret キー一式」を構築
2. SSM / Secrets Manager から最新値を boto3 で取得
3. Lambda の現在 `Environment.Variables` に secrets を merge して
   `update_function_configuration` で上書き
4. AWS Lambda が warm container を **強制的に再生成** するため、新しい値が
   次の invocation から即座に反映される

**`pocket waf ip` と同じ side-channel pattern** です。CFn template の
`Environment.Variables` は deploy 時 snapshot として残り、CFn 視点では
drift しますが、**次の `pocket deploy` で自然と再同期** されます (CFn
template が SSM の最新値を読み直して再注入するため)。

`status-env` は drift 検出専用 (副作用なし)。Lambda 側の env と SSM/SM
側の宣言値を比較し、差分のあるキーだけ表示します。

!!! note "secrets 以外の env (POCKET_STAGE / awscontainer.envs / RDS pointers 等)"
    `reload-env` は **secrets のキーだけ更新** し、その他の env は Lambda の
    現状値を保持します。POCKET_STAGE 等の静的 env を変更したい場合は通常の
    `pocket deploy` を使ってください。

### awscontainer.iam

Lambda execution role に追加で IAM 権限を注入します。`use_s3` / `use_route53` / `secrets.allowed_*_resources` 等の built-in な仕組みでカバーできない権限を、ユーザーが宣言的に与えるための逃げ道です。

```toml
[awscontainer.iam]
managed_policy_arns = [
    "arn:aws:iam::aws:policy/AdministratorAccess",
]

[awscontainer.iam.inline_policies.cross-account-assume]
Version = "2012-10-17"

[[awscontainer.iam.inline_policies.cross-account-assume.Statement]]
Effect = "Allow"
Action = "sts:AssumeRole"
Resource = "arn:aws:iam::*:role/provisioner-role"
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `managed_policy_arns` | list[str] | `[]` | LambdaRole の ManagedPolicyArns に追加する AWS managed policy ARN の list |
| `inline_policies` | dict[str, dict] | `{}` | LambdaRole の Policies に追加する inline policy。key は PolicyName の suffix (`resource_prefix` が前置される)、value は PolicyDocument の dict |

inline_policies の value は標準的な IAM PolicyDocument の形式 (`Version` / `Statement` を含む dict) です。TOML の制約から `Statement` を複数行で書く場合は `[[awscontainer.iam.inline_policies.<name>.Statement]]` 形式の table array を使います。

!!! warning "宣言的な仕組みでカバーできない場合の最終手段"
    まずは `use_s3` / `use_route53` / `use_ses` / `use_sqs` 等の service flag や `[awscontainer.secrets]` の `allowed_sm_resources` / `allowed_ssm_resources` で対応できないかを検討してください。
    `awscontainer.iam` は admin tool 等で広い権限が必要なケース、もしくは magic-pocket が built-in でサポートしていない AWS サービスへの権限が必要な場合の逃げ道です。

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
| `publish` | `"deploy"` \| `"command"` | `"deploy"` | staticfiles の publish 方式（`static=true` 時のみ）。下記参照 |

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

!!! note "publish — 静的 publish を deploy から切り離す"
    DB/KVS の `provisioning = "command"` と同じ思想の staticfiles 版です。

    - `"deploy"`（デフォルト）: `pocket django deploy` / `promote` が
      collectstatic + S3 アップロードを実行します（zero-config）
    - `"command"`: deploy / promote は静的ファイルに一切触れません。
      publish は `pocket django deploystatic` に一任します

    大容量の静的資産（画像・動画等）を out-of-band 管理し、CI からのデプロイでは
    コードのみ、資産の publish は別経路（VM 等から資産変更時のみ）としたい場合に
    使います。

    ```toml
    [awscontainer.django.storages]
    staticfiles = { store = "s3", location = "static", static = true, publish = "command" }
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

[prod.awscontainer.django.settings]
DEFAULT_FROM_EMAIL = '"Production" <noreply@example.com>'
CORS_ALLOWED_ORIGINS = ["https://www.example.com"]
```

`settings.py` での読み込み方法は「[Django連携](django.md#django-settings)」を参照してください。

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
    { path_pattern = "/static/*", ref = "static", versioning = "content_hash" },
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
| `hosted_zone_id_override` | str \| None | None | ホストゾーンIDを明示指定 |
| `redirect_from` | list[RedirectFrom] | `[]` | リダイレクト元ドメイン |
| `routes` | list[Route] | **必須** | ルーティング設定（最低1つ必要） |
| `signing_key` | str \| None | None | 署名付きURL用のmanaged secret名 |
| `token_secret` | str \| None | None | SPA トークン認証用の managed secret 名（`type = "spa_token_secret"`） |
| `managed_assets` | str \| None | None | ステージ別アセットのディレクトリ（下記参照） |
| `waf` | dict \| None | None | WAFv2 IP allowlist を attach（下記 [waf](#waf) 参照） |
| `enable_origin_verify` | bool | `false` | origin 直叩き防止 + 詐称耐性 client IP（下記 [origin verify](#origin-verify-enable_origin_verify) 参照） |

### managed_assets

`favicon.ico` や `robots.txt` など、ステージごとに異なるファイルを CloudFront 経由で配信できます。

```toml
[cloudfront.web]
managed_assets = "assets/managed"
routes = [
    { is_default = true, is_spa = true, origin_path = "/web" },
]
```

ディレクトリ構成:

```
assets/managed/
├── default/           # フォールバック
│   ├── favicon.ico
│   └── robots.txt
├── sandbox/           # sandbox ステージ用
│   ├── favicon.ico    # 開発用アイコン
│   └── robots.txt     # Disallow: /
└── prod/               # 本番用
    ├── favicon.ico
    └── robots.txt     # Allow: /
```

`pocket deploy --stage=sandbox` 実行時:

1. `assets/managed/sandbox/` が存在すればそのディレクトリを使用
2. 存在しなければ `assets/managed/default/` にフォールバック
3. ファイルを S3 の `pocket_managed/` にアップロード
4. ファイルごとに CloudFront の CacheBehavior を自動生成（`/favicon.ico`, `/robots.txt` 等）

ファイル単位のマージは行いません。ステージディレクトリがあればそれだけ、なければ default だけが配信されます。

!!! note "SPA のビルド成果物との分離"
    managed_assets は S3 の `pocket_managed/` プレフィックスに配置されるため、SPA の `build_dir` アップロードとは独立しています。`--delete` による意図しない削除の心配はありません。

!!! note "Django のみ（CloudFront なし）の場合"
    CloudFront を使用しない構成では、同じディレクトリ形式で Django view から配信できます（[Django連携 - ステージ別ファイル配信](django.md#ステージ別ファイル配信-managed_assets) を参照）。

### waf

CloudFront に WAFv2 の **IP allowlist 専用 WebACL** を attach します。
`admin.example.com` のような社内向け管理 UI を「固定 IP 以外からは到達不能」
にする用途を想定しています。

```toml
[cloudfront.admin]
domain = "admin.example.com"
routes = [
    { is_default = true, is_spa = true, origin_path = "/admin" },
]

# block を書くだけで WAF が enable になる (デフォルトは IP allowlist モード)
[cloudfront.admin.waf]
# (optional) AWS managed rules を併用する場合
managed_rule_groups = ["AWSManagedRulesCommonRuleSet"]
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `enable_ip_set` | bool | `true` | IPSet + IP allow rule を生成して IP allowlist で運用 |
| `managed_rule_groups` | list[str] | `[]` | AWS managed rule group 名のリスト |

`[cloudfront.<name>.waf]` block を書くと us-east-1 に `AWS::WAFv2::WebACL` が
CFn で作成され、CloudFront distribution の `WebACLId` に attach されます。
block 自体が無い場合は WAF 未 attach (既存挙動と完全互換)。

#### モード 1: IP allowlist (デフォルト)

`enable_ip_set = true` (省略可) の場合、`AWS::WAFv2::IPSet` (Scope=CLOUDFRONT,
Addresses=`[]`) と「IPSet にマッチしたら Allow」ルールを生成し、
`DefaultAction = Block` にします。**初回 deploy 直後は IPSet が空なので
deny-all 状態**。`pocket waf ip add self ...` で CIDR を投入してください。

社内 admin UI を固定 IP から到達可能にする、というのが想定ユースケースです。

#### モード 2: managed rules のみ (`enable_ip_set = false`)

IP 制限はしたくないが AWS managed rules による検査だけ走らせたい場合は:

```toml
[cloudfront.admin.waf]
enable_ip_set = false
managed_rule_groups = ["AWSManagedRulesCommonRuleSet"]
```

このとき:

- `AWS::WAFv2::IPSet` は **生成されない** (`pocket waf ip ...` CLI も使用不可)
- `DefaultAction = Allow` で、managed rules にマッチした怪しいリクエスト
  のみ block される (「許可ベース + 攻撃シグネチャだけ弾く」構成)

`enable_ip_set = false` でかつ `managed_rule_groups` も空、という構成は
WebACL が何もしない pass-through 状態になるので、settings の validator が
エラーで reject します。

#### IP リテラルは toml に書かない

`ip_allow_list_default` のような「IP アドレスを toml で宣言する」フィールドは
意図的に提供していません。`pocket.toml` に IP リテラルを書いた場合は
`extra = "forbid"` で validation エラーになります。

理由は **真実源を一系統に絞るため**:

- 実 IP リストは `pocket waf ip ...` CLI で日常的に更新される (社内 IP 追加、
  外出先からの一時許可など、操作頻度が高い)
- toml にも書けるようにすると、toml と IPSet の値が drift し「toml に書いた
  はずなのに反映されていない」「CLI で消したはずなのに再 deploy で復活」の
  事故が起きる

CFn template も Addresses=`[]` で固定し、再 deploy のたびに空が出力されます。
CFn 視点では IPSet の中身は常に drift しますが、これは仕様です。CLI が
side-channel で書いた CIDR は CFn update で消えません。

#### CLI: `pocket waf ip ...`

IPSet の中身 (実際の CIDR) は専用 CLI で更新します。`update_ip_set` boto3
を直接叩くため、CFn stack を回さずに秒オーダーで反映されます。

```bash
# 一覧表示
pocket waf ip list --name admin --stage prod

# 自分の Global IP を /32 で追加 (checkip.amazonaws.com → ipify fallback)
pocket waf ip add self --name admin --stage prod

# 任意 CIDR を追加
pocket waf ip add 203.0.113.0/24 --name admin --stage prod

# 削除
pocket waf ip remove 203.0.113.0/24 --name admin --stage prod

# 全削除 (deny-all 状態に戻す。確認プロンプトあり)
pocket waf ip clear --name admin --stage prod
```

初回 `pocket deploy` の直後は IPSet が空 (deny-all) なので、最低 1 件 CIDR
を追加するまで CloudFront は全リクエストを 403 で拒否します。デプロイ直後に
`pocket waf ip add self ...` を実行してください。

#### 必要な IAM 権限

`pocket.toml` に `[cloudfront.<name>.waf]` block を 1 つでも書くと、
`wafv2:*` が `pocket permissions list` の出力に追加されます
([AWS 権限](../permissions/aws.md#cloudfront-wafcloudfrontnamewaf-使用時) を
参照)。

### origin verify (enable_origin_verify)

CloudFront 配下の origin (lambda / API Gateway、将来は Fargate/ALB) で、
**アクセス元 client IP を詐称耐性をもって取得**し、かつ **origin への直叩きを
禁止**する仕組みを一括で有効化します。

```toml
[cloudfront.web]
routes = [
    { type = "lambda", handler = "wsgi", is_default = true },
]
enable_origin_verify = true
```

`enable_origin_verify = true` で deploy すると magic-pocket が次の 3 点を turnkey で
構成します。secret header 名 / env 名 / viewer IP header 名はすべて **magic-pocket の
内部実装詳細**で、利用者が知る必要はありません (repo 跨ぎの名前合わせを発生させない)。

1. **secret の自動生成・管理**: managed secret `POCKET_ORIGIN_VERIFY_SECRET`
   (`type = "origin_verify_secret"`) を自動注入し、生成・保存 (SM/SSM)・IAM・Lambda
   runtime env 注入の既存経路に乗せます。利用者が secret を宣言する必要はありません。
2. **origin 直叩き禁止**: CloudFront → origin のリクエストに secret custom header
   (`X-Pocket-Origin-Verify`) を付与します。viewer が同名 header を送っても CloudFront
   が上書きするため詐称不可。同じ secret 値が Lambda runtime env にも入るので、
   バックエンドは「自分宛のリクエストが CloudFront 経由か」をバックエンド非依存に
   判定できます (Lambda でも Fargate でも同じコード)。
3. **検証 + `REMOTE_ADDR` 正規化 middleware**: 同梱の
   `pocket.django.origin_verify.OriginVerifyMiddleware` が secret header を検証し、
   CloudFront 経由のときだけ詐称耐性のある viewer IP を `REMOTE_ADDR` に上書きします。

#### 詐称耐性 client IP (デフォルト ON、flag 非依存)

lambda route には、`enable_origin_verify` の有無に関わらず CloudFront Function が
`event.viewer.ip` (CloudFront が TCP 接続から取得する viewer IP。viewer が詐称不可) を
`X-Pocket-Viewer-Ip` header に載せて origin に転送します。これは純粋に加算的で
キャッシュにも影響しないため、デフォルト挙動です。

- `requestContext.sourceIp` (API GW) は CloudFront エッジの IP で真の client ではなく、
  API GW 固有なので Fargate 移行で消えます。
- `X-Forwarded-For` 左端は viewer が prepend して詐称可能です。
- magic-pocket は managed `AllViewerExceptHostHeader` origin request policy を使い続け
  (API GW の Host 整合性を壊さないため)、viewer IP は **CloudFront Function が付与する
  通常 header** として転送します。origin request policy の差し替えは行いません。

#### Django middleware の組み込み

`MIDDLEWARE` の **最前段** に追加してください (`REMOTE_ADDR` を読む django-axes /
DRF throttling / ratelimit / access log より前に走らせる必要があるため)。

```python
MIDDLEWARE = [
    "pocket.django.origin_verify.OriginVerifyMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # ...
]
```

middleware の挙動:

| 状況 | 挙動 |
|------|------|
| env secret 未設定 (local/dev、CloudFront 無し) | **no-op**。生の `REMOTE_ADDR` を passthrough |
| secret header が一致 (CloudFront 経由) | `X-Pocket-Viewer-Ip` を `REMOTE_ADDR` に正規化 |
| secret header が無い / 不一致 (origin 直叩き) | **403** で拒否 |

!!! note "直叩き時に `REMOTE_ADDR = None` にしない理由"
    `REMOTE_ADDR` を読む consumer (DRF throttle の `get_ident`、django-axes、access
    log) は str 前提で、`None` は 500 を誘発します。`enable_origin_verify` 有効 +
    secret 無しは「origin 直叩き」なので 403 で弾くのが綺麗です (理想は API Gateway
    段で Django に到達させない)。無効時 (local/dev) は生 `REMOTE_ADDR` を passthrough
    するので local は壊れません。

!!! note "secret rotation"
    secret は managed secret なので `pocket` の rotate 経路で再生成し、`pocket deploy`
    で CloudFront origin header (CFn) と Lambda env (SM/SSM) の両方が同値に更新されます。
    rotate 直後は、新 header を受け取る warm Lambda がまだ旧 env を保持する一瞬の窓で
    403 になり得ます (cold start で解消)。無停止 rotation が必要な場合は別途検討します。

### redirect_from

```toml
[prod.cloudfront.main]
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
[prod.cloudfront.main]
domain = "www.example.com"
routes = [
    { is_default = true, is_spa = true },
    { path_pattern = "/assets/*", versioning = "content_hash" },
]
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `type` | `"s3"` \| `"lambda"` | `"s3"` | ルートの種類 |
| `handler` | str \| None | None | Lambda handler 名（`type = "lambda"` 時必須。WSGI / ASGI / Rust / Go 等、API Gateway 経由で公開される Lambda なら何でも） |
| `origin_path` | str \| None | None | S3 オリジンパス（`type = "s3"` 時**必須**。`type = "lambda"` では指定不可） |
| `path_pattern` | str | `""` | パスパターン |
| `is_default` | bool | `false` | CloudFront の DefaultCacheBehavior として使用 |
| `is_spa` | bool | `false` | SPA用の設定（フォールバックHTML対応） |
| `versioning` | `"content_hash"` \| `"deploy_hash"` \| None | None | バージョニング方式。`content_hash` = ファイル内容ハッシュ (ManifestStaticFilesStorage)、`deploy_hash` = git hash で URL prefix 付与 |
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
    - `is_spa` と `versioning` は同時に設定できません。
    - `path_pattern` は空でないルートは `/` で始まる必要があります。
    - `signed = true` のルートには、distribution に `signing_key` の設定が必要です。
    - `type = "lambda"` のルートでは `origin_path`, `is_spa`, `versioning`, `signed`, `require_token`, `build`, `build_dir` は使用できません。`is_default = true` は許可されており、Django 単体構成（全リクエストを API Gateway に流す）で利用できます。
    - 旧 `type = "api"` は廃止されました。`type = "lambda"` を使ってください（起動時に分かりやすいエラーが出ます）。
    - 旧 `is_versioned` は廃止されました。`versioning = "content_hash"` を使ってください。
    - `handler` は `awscontainer.handlers` に定義されている必要があり、`apigateway` が設定されていなければなりません。
    - `build` を指定する場合は `build_dir` が必須です。
    - `require_token = true` のルートには `is_spa = true` が必須です。distribution に `token_secret` の設定が必要です。

### Django 単体構成（CloudFront → Lambda のみ）

SPA を持たず、Django テンプレートで完結するプロジェクトを CloudFront 経由で配信する構成です。
`is_default = true` の `type = "lambda"` ルートを 1 つだけ定義します。

```toml
[awscontainer.handlers.wsgi]
command = "pocket.django.lambda_handlers.wsgi_handler"
apigateway = {}

[prod.cloudfront.web]
domain = "www.example.com"
routes = [
    { is_default = true, type = "lambda", handler = "wsgi" },
]
```

CloudFront の `DefaultCacheBehavior` が API Gateway オリジンを直接ターゲットにし、`X-Forwarded-Host`
が付与されるため、Django 側ではカスタムドメインがそのまま `request.get_host()` で取得できます。

### バージョニング（キャッシュバスティング）

`versioning` フィールドで静的アセットのキャッシュバスティング方式を選択できます。

#### `content_hash` — ファイル内容ハッシュ

Django の `ManifestStaticFilesStorage` と組み合わせる方式。`collectstatic` 時にファイル名にハッシュが付与されるため、ファイル名が変わればキャッシュが自然に更新されます。

```toml
[cloudfront.web]
routes = [
    { is_default = true, is_spa = true, origin_path = "/app" },
    { path_pattern = "/static/*", ref = "static", versioning = "content_hash", origin_path = "/static" },
]
```

#### `deploy_hash` — デプロイ時 git hash

`ManifestStaticFilesStorage` を使わず、デプロイ時の git hash を URL prefix に付与する方式。manifest 計算が不要で高速、動画など大きなファイルにも適しています。

??? example "deploy_hash の完全な pocket.toml 例"
    ```toml
    [general]
    region = "ap-northeast-1"
    stages = ["sandbox", "prod"]
    project_name = "myproject"

    [s3]

    [awscontainer]
    dockerfile_path = "pocket.Dockerfile"

    [awscontainer.handlers.wsgi]
    command = "pocket.django.lambda_handlers.wsgi_handler"
    apigateway = {}

    [awscontainer.handlers.management]
    command = "pocket.django.lambda_handlers.management_command_handler"
    timeout = 600

    [awscontainer.django.storages]
    default = { store = "filesystem" }
    staticfiles = { store = "s3", static = true, distribution = "web", route = "static" }

    [awscontainer.secrets]
    store = "ssm"

    [awscontainer.secrets.managed]
    SECRET_KEY = { type = "password", options = { length = 50 } }

    [sandbox.cloudfront.web]
    domain = "sandbox.myproject.example.com"
    routes = [
        { type = "lambda", handler = "wsgi", is_default = true },
        { path_pattern = "/static/*", ref = "static", versioning = "deploy_hash", origin_path = "/static" },
    ]
    ```

    Django settings.py:

    ```python
    import os

    DEPLOY_HASH = os.environ.get("DEPLOY_HASH", "dev")
    STATIC_URL = f"static/{DEPLOY_HASH}/"

    from pocket.django.utils import get_storages
    STORAGES = get_storages()
    ```

    デプロイ:

    ```bash
    pocket django deploy --stage sandbox -y
    ```

動作:

1. pocket がデプロイ時に `git rev-parse --short HEAD` で hash を取得（`DEPLOY_HASH` 環境変数があればそちらを優先）
2. Lambda 環境変数 `DEPLOY_HASH` に自動注入
3. CloudFront Function が自動生成され、`/static/{hash}/foo.js` → `/static/foo.js` に変換してオリジンに転送
4. CloudFront のキャッシュキーはフル URL (hash 込み) なので、デプロイごとにキャッシュが自然に更新される
5. `versioned_max_age`（デフォルト 1 年）の長期キャッシュが付与される

Django 側は settings.py に以下を書くだけです:

```python
DEPLOY_HASH = os.environ.get("DEPLOY_HASH", "dev")
STATIC_URL = f"static/{DEPLOY_HASH}/"

from pocket.django.utils import get_storages
STORAGES = get_storages()
```

`get_storages()` は deploy_hash route を検出し、Lambda 上では自動的に `StaticFilesStorage` を選択します（`STATIC_URL` のパスがそのまま `{% static %}` タグの出力になります）。`deploystatic` 時は S3 backend が使われるため、アップロードは正常に動作します。

S3 へのアップロードは hash prefix なし（`/static/foo.js`）のまま行います。`collectstatic` は通常の `StaticFilesStorage` で OK です（`manifest = true` は不要）。

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
    { path_pattern = "/api/*", type = "lambda", handler = "wsgi" },
]
```

`type = "lambda"` のルートでは以下が自動設定されます:

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
    { path_pattern = "/api/*", type = "lambda", handler = "wsgi" },
]
```

トークンの形式は `{user_id}:{expiry_unix}:{hmac_hex}` です。
Django 側では `pocket.django.spa_auth` モジュールでトークンの生成・検証・Cookie 設定が可能です。
詳細は「[実行環境 - SPA トークン認証](runtime.md#spa-トークン認証)」を参照してください。

!!! warning "login_path の除外"
    `login_path`（デフォルト: `/api/auth/login`）はトークン検証の対象外にする必要があります。
    `type = "lambda"` ルートで `/api/*` を API Gateway にルーティングしている場合、ログインエンドポイントは Lambda 側で処理されるためトークン検証は行われません。

---

## scheduler

EventBridge Scheduler (`AWS::Scheduler::Schedule`) で Lambda handler を定期実行します。
旧来の EventBridge Rule (`AWS::Events::Rule`) ではなく、AWS が現在推奨する EventBridge Scheduler を採用しています。Lambda Permission が不要、1 schedule = 1 リソース、IAM role ベースで invoke するため、構成がシンプルでスケーラビリティも高いです。

### 基本

```toml
[scheduler.schedules.rotate_logs]
rate = "1 hour"
handler = "worker"
input = { task = "rotate_logs" }

[scheduler.schedules.daily_digest]
scheduler = "pocket.django.management_lambda_scheduler"
cron = "0 18 * * ? *"
handler = "management"
manage = "send_daily_digest --verbose"
```

各 entry は `[scheduler.schedules.{key}]` の dict 形式で書きます。`{key}` (例: `rotate_logs`) はそのまま CloudFormation logical ID と物理名の素材になるため、**並び順に依存しない安定した命名**が得られます。

### entry の共通フィールド

| フィールド | 型 | デフォルト | 説明 |
|---|---|---|---|
| `scheduler` | `"pocket.lambda_scheduler"` \| `"pocket.django.management_lambda_scheduler"` | `pocket.lambda_scheduler` | スケジューラ実装。default は汎用 Lambda |
| `cron` | str \| None | None | EventBridge cron 式（`cron(...)` のラッパー部分は不要、中身だけ書く） |
| `rate` | str \| None | None | EventBridge rate 式（`rate(...)` のラッパー部分は不要） |
| `handler` | str | **必須** | `awscontainer.handlers.{key}` の key を指定 |

`cron` と `rate` は **どちらか一方を必ず指定**します（両方や両方無しはエラー）。

### `pocket.lambda_scheduler` (default)

汎用の Lambda invoke。任意の dict を `input` フィールドで EventBridge Target Input にそのまま渡します。

```toml
[scheduler.schedules.rotate_logs]
rate = "1 hour"
handler = "worker"
input = { task = "rotate_logs", target = "primary" }
```

handler 側では `event["task"]` のように直接読めます。

| フィールド | 型 | デフォルト | 説明 |
|---|---|---|---|
| `input` | dict | `{}` | EventBridge Target Input としてそのまま渡される dict |

### `pocket.django.management_lambda_scheduler`

Django management command を呼び出すショートカット。`manage` に shell-style コマンドラインをそのまま書きます。

```toml
[scheduler.schedules.daily_digest]
scheduler = "pocket.django.management_lambda_scheduler"
cron = "0 18 * * ? *"
handler = "management"
manage = "send_daily_digest some_param --verbose --batch-size 100"
```

| フィールド | 型 | デフォルト | 説明 |
|---|---|---|---|
| `manage` | str | **必須** | shell-style の management command (例: `"send_daily_digest --verbose"`) |

**制約**: 参照する `handler` は `command = "pocket.django.lambda_handlers.management_command_handler"` でなければなりません（deploy 前にバリデーションエラーになります）。

実装的には、Lambda には `{"manage": "<コマンド文字列>"}` が渡され、handler 側で `shlex.split` → `call_command` を行います。既存の `{command, args, kwargs}` 形式と完全に共存しており、後方互換性は壊しません。

### ステージ別 schedule

dict 形式は **deep merge** が効くため、entry 単位で stage オーバーライド・追加・調整が自然に書けます。

```toml
# 全 stage 共通
[scheduler.schedules.rotate_logs]
rate = "1 hour"
handler = "worker"
input = { task = "rotate_logs" }

[scheduler.schedules.daily_digest]
scheduler = "pocket.django.management_lambda_scheduler"
cron = "0 18 * * ? *"
handler = "management"
manage = "send_daily_digest"

# sandbox では rotate_logs の頻度だけ落とす
[sandbox.scheduler.schedules.rotate_logs]
rate = "1 day"

# prod だけで動く追加 schedule
[prod.scheduler.schedules.month_end_invoice]
scheduler = "pocket.django.management_lambda_scheduler"
cron = "0 0 L * ? *"
handler = "management"
manage = "send_monthly_invoice"
```

### 命名のコツ

- **handler key** は「何をする Lambda か」(`management`, `worker`, `mailer`)
- **entry key** は「いつ動くか」(`nightly`, `hourly`, `month_end`, `rotate_logs`)

`cron` のような AWS 用語を key に使うと、cron 式そのものとの混同が起きやすいので避けてください。

### CloudFormation リソース構成

各 entry に対して 1 つの `AWS::Scheduler::Schedule` が出力されます。Lambda Permission は不要で、共有の `AWS::IAM::Role` (`{resource_prefix}scheduler`) が EventBridge Scheduler に対して `lambda:InvokeFunction` を許可します。`Resource` は schedule で参照されている Lambda 関数 ARN に絞り込まれます。

### wsgi handler のウォームアップは非対応

`wsgi_handler` は API Gateway proxy event 形式を期待するため、scheduler が渡す任意 input dict では呼び出せません。Lambda のコールドスタートを抑えたい場合は **Provisioned Concurrency** を利用してください（reserved_concurrency や warmup の handler を別途書くより堅牢です）。
