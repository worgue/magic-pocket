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
[container.main]      # Lambda設定（全ステージ共通）
[cloudfront]        # CloudFront設定（全ステージ共通）
[scheduler]         # EventBridge Scheduler 設定（全ステージ共通）

[dev.container.main]  # dev ステージ固有のLambda設定
[prod.s3]            # prod ステージ固有のS3設定
```

!!! info "ステージ毎の設定"
    `[neon]` のようにステージ名なしで書くと、全ステージに適用されます。

    `[dev.neon]` のようにステージ名をプレフィックスにすると、そのステージのみに適用されます。
    ステージ固有の設定は、共通設定にディープマージされます。`[general]` を含む全セクションが対象です。

!!! info "ステージ名で挙動が変わる設定はありません"
    pocket は **ステージ名に意味を与えません**。`prod` という名前だからデフォルトが安全側に変わる、といった挙動は一切なく、すべてのステージで同じデフォルトが適用されます。

    ステージ名は `general.stages` に書いた任意の文字列で、プロジェクトによって `prod` / `production` / `live` と揺れます。ツールが特定の名前を特別扱いすると、命名が違うだけで意図した設定が効かなくなり、**しかも効いていないことに気づけません**。

    本番だけ値を変えたい場合は、上記のステージ上書きで**明示**してください。

    ```toml
    [dsql]
    # dev はこのまま

    [prod.dsql]
    deletion_protection = true
    ```

    逆に「全ステージで安全側にすべき」設定は、ステージで分けずにデフォルト自体をそう定めています（例: `[rds.backup]` の `retention_days` は AWS 既定の 1 日ではなく 35 日）。

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

ローカル環境で使うDjango設定を記述します。設定項目は [`container.main.django`](#containerdjango) と同じです。

```toml
[general.django_fallback.storages]
default = { store = "filesystem" }
staticfiles = { store = "filesystem", static = true }
```

---

## vpc

VPC設定をトップレベルで定義します。`[vpc]` を定義すると、`container` と `rds` は自動的に VPC 内に配置されます。

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

### use_vpc（container / rds）

`container` や `rds` セクションで `use_vpc` を指定すると、VPC の利用を明示的に制御できます。

| 値 | 動作 |
|---|------|
| 未指定 | auto: `[vpc]` があれば VPC 内に配置 |
| `true` | 必須: `[vpc]` がなければエラー |
| `false` | VPC を使わない |

```toml
[vpc]
ref = "main"
zone_suffixes = ["a"]

[container.main]
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

S3 Lifecycle ルールを宣言できます。`pocket resource s3 create` で冪等に reconcile されます。

```toml
# 旧バージョンの期限切れ (versioning 有効時の掃除)
[[s3.lifecycle_rules]]
id = "expire-non-current-static"
prefix = "static/"
noncurrent_version_expiration_days = 1

# 現行オブジェクトの日数削除 (trash prefix の自動削除など)
[[s3.lifecycle_rules]]
id = "trash-expire"
prefix = "trash/"
expiration_days = 30
```

| フィールド | 型 | 説明 |
|-----------|------|------|
| `id` | str | ルール ID（バケット内で一意） |
| `prefix` | str | 適用対象の prefix（`""` で全オブジェクト） |
| `expiration_days` | int (≥1) \| None | 現行バージョンを削除するまでの日数（Expiration.Days） |
| `noncurrent_version_expiration_days` | int (≥1) \| None | 旧バージョンを期限切れにするまでの日数 |

`expiration_days` と `noncurrent_version_expiration_days` は少なくとも一方の指定が必要です（両方の同時指定も可）。

!!! note "宣言的に管理されます"
    lifecycle は versioning と同じく宣言的に reconcile されます。pocket.toml に無いルール（コンソール等で手動追加したものを含む）は次回の reconcile で削除されるため、ルールは必ず pocket.toml に宣言してください。

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
    事前に保存された `DATABASE_URL`（[stored mode](#containersecretsuser) の user secret）
    を読むだけになります。

    ```toml
    [dev.neon]
    project_name = "dev-myproject"
    provisioning = "command"

    [dev.container.main.secrets.user]
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
    従来の computed mode（`[container.main.secrets.managed]` に
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

[container.main.secrets.managed]
REDIS_URL = { type = "upstash_redis_url" }

[container.main.django.caches]
default = { store = "redis" }
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `budget` | int | `20` | 月額上限（ドル）。最低値の $20 がデフォルト |
| `provisioning` | `"deploy"` \| `"command"` | `"deploy"` | provisioning を deploy が行うか `pocket resource upstash store-url` に委ねるか（[neon](#neon) の同名フィールド参照） |

!!! info "provisioning = command で credential なしデプロイ"
    `[upstash] provisioning = "command"` にすると deploy は Upstash に触れません。`REDIS_URL` を
    `[container.main.secrets.user]` に `{ type = "upstash_redis_url" }`（stored mode）で宣言し、
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

!!! warning "PostgreSQL 互換だが、通常の PostgreSQL の常識では書けない"
    Aurora DSQL は PostgreSQL 互換の分散データベースですが、**スキーマとクエリの
    書き方に無視できない制約があります**。pocket はクラスターの作成までを担当し、
    スキーマ設計はアプリ側の責任なので、`[dsql]` を使う前に把握しておいてください。

    | 項目 | 通常の PostgreSQL | Aurora DSQL |
    |------|------------------|-------------|
    | `FOREIGN KEY` | 使える | **使えない**（参照整合性はアプリ層で担保する） |
    | `UPSERT`（`INSERT ... ON CONFLICT`） | 使える | **使えない**（delete + insert で代替する） |
    | `SERIAL` / シーケンス | 使える | 非推奨。主キーは `uuid DEFAULT gen_random_uuid()` 等にする |
    | インデックス作成 | `CREATE INDEX`（同期） | **`CREATE INDEX ASYNC`**（非同期。発行直後は未有効の可能性がある） |
    | トランザクション内の DDL | 制限なし | **DDL と DML は別トランザクション。1 トランザクションに DDL は 1 文まで** |
    | 1 トランザクションの更新行数 | 制限なし | **3,000 行まで**（`INSERT` / `UPDATE` / `DELETE`。大量更新はバッチ分割が必要） |
    | 同時実行制御 | ロック（MVCC） | **楽観的並行性制御（OCC）**。コミット時に `40001` で失敗しうるためリトライ設計が要る |
    | 接続 | パスワード等 | **IAM 認証トークン（最大 15 分）**。プールは接続を短命化し、常駐プロセスは期限前に再生成する |

    **3,000 行制限は「更新できる行数」であって、`SELECT` が返せる行数の制限ではありません**
    （混同されがちです）。

    一次情報は AWS の
    [Migrating from PostgreSQL to Aurora DSQL](https://docs.aws.amazon.com/aurora-dsql/latest/userguide/working-with-postgresql-compatibility-unsupported-features.html)
    と [Cluster quotas and database limits](https://docs.aws.amazon.com/aurora-dsql/latest/userguide/CHAP_quotas.html) です。
    実際に動く構成は [サンプルプロジェクト](examples.md) の `example-dsql` を参照してください
    （IAM トークンの生成とプール設定、`CREATE INDEX ASYNC` を含む migration の実例があります）。

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `deletion_protection` | bool | `false` | 削除保護の有効化 |

!!! tip "prod だけ削除保護を有効にする"
    `deletion_protection` のデフォルトは全ステージ共通です（pocket はステージ名で挙動を変えません。[基本構造](#_1)を参照）。本番だけ有効にする場合はステージ上書きで明示してください。

    ```toml
    [dsql]
    # dev はこのまま

    [prod.dsql]
    deletion_protection = true
    ```

Lambda 環境変数として `POCKET_DSQL_ENDPOINT` と `POCKET_DSQL_REGION` が自動設定されます。
`set_envs()` の呼び出し時に、IAM 認証トークンが `POCKET_DSQL_TOKEN` に設定されます。

### 定期バックアップ

DSQL に組み込みの自動バックアップ（PITR や日次スナップショット）はありません。**何も宣言しなければ、誤削除・論理破壊からの復元手段はゼロです**（宣言が無い stage では deploy が毎回警告します）。定期バックアップはトップレベルの [`[backup.dsql]`](#backup) で宣言してください（1 行書くだけで daily / weekly / monthly の GFS 階層保持が有効になります）。

単発のバックアップは `pocket resource dsql backup` で取得できます（[CLI リファレンス](cli.md) 参照）。**`--retention-days` を省略した単発バックアップは、`[backup.dsql]` の最長階層（monthly）の `delete_after_days` を継承します**（宣言が無い stage では 1095 日）。定期と単発で長期保持ポリシーが一致します。

復元は AWS Backup が常に**新規クラスター**を作成する形で行われます（元のクラスターは変更されません）。`pocket resource dsql restore` を参照してください。

!!! info "endpoint の publish（deploy の外から endpoint を引く）"
    DSQL の cluster identifier は AWS 自動生成のため、endpoint
    （`{id}.dsql.{region}.on.aws`）は命名規約から導出できません。このため deploy は
    cluster の provision 完了時に、endpoint を stored user secret の正準パス
    `/{stage}-{project}-{namespace}-user/dsql_endpoint`（`secrets.store` が `sm` の
    場合は先頭 `/` なしの Secrets Manager secret）へ書き込みます。cluster を再作成
    した場合も deploy が上書きし、常に deploy 済み実体を反映します（値が同じなら
    書き込みません）。`pocket destroy` で cluster を削除すると publish も削除されます。

    deploy の外の消費者（migration ツール・外部 provisioner 等）は
    `pocket.naming` で正準名を導出して読み出せます:

    ```python
    from pocket.naming import DSQL_ENDPOINT, stored_user_secret_name

    name = stored_user_secret_name(
        project="myprj", stage="dev", secret_type=DSQL_ENDPOINT, store="ssm"
    )
    # -> "/dev-myprj-pocket-user/dsql_endpoint"
    ```

    `store` は対象プロジェクトの `[container.main.secrets].store`（未設定なら既定の
    `"sm"`）に合わせてください。

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

[container.main]
dockerfile_path = "pocket.Dockerfile"
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `managed` | bool | `true` | `true` = pocket がクラスタを作成・管理、`false` = 既存クラスタを参照 |
| `min_capacity` | float | `0.5` | Serverless v2 最小キャパシティ（ACU）。`managed = true` のみ |
| `max_capacity` | float | `2.0` | Serverless v2 最大キャパシティ（ACU）。`managed = true` のみ |
| `snapshot_identifier` | str \| None | None | 初回作成時に復元する snapshot の ID / ARN。`managed = true` のみ |
| `backup.retention_days` | int | `35` | 自動バックアップ（PITR）の保持日数（1〜35）。`managed = true` のみ |
| `database` | str \| None | None | DB 名の上書き。未指定なら `{stage}_{project}`（他リソース名と同じ順序）。`managed = true` のみ |
| `secret_arn` | str \| None | None | 既存 RDS の Secrets Manager ARN。`managed = false` 時必須 |
| `security_group_id` | str \| None | None | 既存 RDS の SG ID。`managed = false` 時必須 |

!!! info "自動バックアップ（PITR）の保持期間"
    Aurora の自動バックアップは常時有効で、保持期間内の任意の時点に復元（PITR）できます。pocket は保持日数を **35 日**（ネイティブ上限）で作成します（AWS 既定は 1 日）。1 日だと「PITR があるからいつでも戻せる」という理解と実態（24 時間しか遡れない）が乖離するためです。

    ```toml
    [rds.backup]
    retention_days = 14   # 短くしたい場合
    ```

    保持日数はクラスターのネイティブ属性なので、追加リソースも追加権限も不要です。Aurora のバックアップストレージはクラスター容量までは無課金のため、変更（update / delete）の少ないワークロードでは保持を伸ばしても通常は増分コストが生じません。既存クラスターの値が設定と異なる場合は、次の deploy で `ModifyDBCluster`（`ApplyImmediately`）により収束します。

    35 日を超える長期保持（週次・月次）は PITR では実現できず、トップレベル [`[backup.rds]`](#backup)（AWS Backup の backup plan）を宣言して担わせます。PITR のローリングウィンドウと週次・月次スナップショットは独立した仕組みで、「PITR 期間が切れたら自動でスナップショットに移る」わけではなく並走します。PITR 自体は `[backup.rds]` の宣言と無関係に常に有効です（このため `[backup.rds]` に daily 階層はありません）。

!!! info "DATABASE_URL の設定"
    `[container.main.secrets.managed]` に `DATABASE_URL = { type = "rds_database_url" }` または `{ type = "auto_database_url" }` を定義してください。
    Lambda の cold start 時に `POCKET_RDS_SECRET_ARN` から DATABASE_URL が動的に構築されます。

!!! info "master password 自動ローテーションへの追従"
    RDS は `ManageMasterUserPassword=True` で作成され、master password は AWS により自動ローテーション（デフォルト 7 日周期）されます。`get_databases()` は `[rds]` 設定時に RDS 専用の DB backend (`pocket.django.db_backends.rds`) を自動選択し、**接続確立時に認証エラー（PostgreSQL SQLSTATE class 28）を検知すると Secrets Manager から最新パスワードを取り直して 1 度だけ自動再接続します**。

    これにより、ローテーション直後に warm Lambda が古いパスワードで失敗し続けることはなく、cold start を待たずに自己修復します（手動介入不要）。既に確立済みの接続は PostgreSQL の仕様上ローテーション後も生き続けるため、影響を受けるのは再接続が必要になった瞬間だけです。

!!! warning "制約事項"
    - managed VPC（`manage=true`）では `zone_suffixes` が 2 つ以上必要です（DB Subnet Group に最低 2AZ 必要）。
    - 外部 VPC（`manage=false`）ではサブネット数は自動検出されます。
    - `container` も同じ VPC に配置されている必要があります。
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
    `managed = false` でも `managed = true` と同じく、Lambda 起動時に `POCKET_RDS_SECRET_ARN` から `DATABASE_URL` が動的に構築されます。`[container.main.secrets.managed]` に `DATABASE_URL` を定義する必要はありません（pocket が自動で注入します）。

!!! warning "制約"
    - `managed = false` では `min_capacity`, `max_capacity`, `snapshot_identifier` は使用できません
    - `secret_arn` と `security_group_id` は `managed = false` でのみ使用可能です（`managed = true` で指定するとエラー）
    - VPC 設定 (`[vpc]`) は不要です（Lambda と RDS が同一 VPC にいる前提で、SG ingress のみで接続します）

---

## backup

DB 層の定期バックアップ（AWS Backup の backup plan）です。pocket が管理する AWS ネイティブ DB をエンジン別に `[backup.dsql]` / `[backup.rds]` で宣言します（opt-in。未宣言なら AWS Backup は使いません）。宣言だけで **GFS（Grandfather-Father-Son）階層保持**が既定で有効になります: 短期は細かく、古くなるほど間引いて長期保持する定石構成です。

```toml
[backup.dsql]         # この 1 行で dsql の GFS 既定（daily / weekly / monthly）
[backup.rds]          # この 1 行で rds の GFS 既定（weekly / monthly。daily は PITR が担当）
```

既定の階層は以下です:

| 階層 | dsql | rds (managed) | 既定スケジュール |
|------|------|---------------|----------------|
| 直近の細かい復元点 | `daily`（35 日保持） | **PITR**（[`[rds.backup]`](#rds)、35 日・秒単位） | daily: 毎日 3:00 |
| weekly | 365 日保持、90 日で cold storage へ | 365 日保持 | 毎週日曜 4:00 |
| monthly | 1095 日（3 年）保持、90 日で cold storage へ | 1095 日（3 年）保持 | 毎月 1 日 5:00 |

各階層は上書きできます（フィールドは `cron` / `delete_after_days`、dsql のみ `cold_storage_after_days`）:

```toml
[backup]
deletable = false     # true で pocket からのバックアップデータ削除を許可
timezone = "UTC"      # 全階層の cron を解釈するタイムゾーン

[backup.dsql]
[backup.dsql.monthly]
delete_after_days = 1825   # monthly だけ 5 年に伸ばす

[backup.rds]
[backup.rds.weekly]
cron = "0 4 ? * 7 *"       # 土曜に変更
```

deploy が AWS Backup の vault（`pocket-backup`）・サービスロール・エンジン別の backup plan / selection（`{stage}-{project}-{namespace}-backup-dsql` / `-rds`）を冪等に provision します。plan をエンジン別に分けるのは、AWS Backup の rule が selection 全体に一律適用されるためです（rds に daily を作らない・cold storage を使わない、をエンジン単位でしか表現できません）。

!!! warning "backup 関連の宣言は厳密に検証されます"
    「書いたのに守られていない」を防ぐため、以下はすべて警告ではなく**エラー停止**します:

    - `[backup]` だけ書いてエンジン宣言（`[backup.dsql]` / `[backup.rds]`）が無い
    - `[backup.neon]` など対象外エンジンの宣言（外部サービス DB は AWS Backup の対象外。各サービス側のバックアップ機能に依存し、長期保持が必要なら論理ダンプを S3 へ書き出す運用を別途検討してください）
    - `[backup.dsql]` を宣言したのに `[dsql]` が無い（`[backup.rds]` と `[rds]` も同様。`managed = false` の rds も不可）
    - `[backup.rds]` への `cold_storage_after_days`（Aurora の snapshot は AWS Backup の cold storage 非対応）
    - `[backup.rds.daily]`（直近 35 日は PITR の責務）

!!! info "PITR とスナップショットの関係（rds）"
    PITR はローリングウィンドウ（直近 35 日を秒単位で復元）、この plan は離散スナップショット（週次・月次を長期保持）で、両者は独立に並走します。「PITR 期間が切れたら自動でスナップショットに移る」わけではありません。PITR は `[backup.rds]` の宣言と無関係に常に有効です。なお Aurora の PITR ストレージはクラスター容量まで無課金ですが、**AWS Backup のスナップショットには無料枠が無く** GB 単価で課金されます。

!!! info "保存先は backup vault（S3 バケットではありません）"
    AWS Backup の保存先は AWS 管理の **backup vault**（pocket 管理の `pocket-backup`）で、任意の S3 バケットを保存先に指定することはできません。ただし vault のライフサイクルが「即時取り出し可能な warm → cold storage → 削除」を表現するため、S3 の Standard → Glacier → 有効期限と同じ保持ポリシーを組めます。バックアップの中身をユーザー側で圧縮することはできません（スナップショットはストレージ層で増分保存され、コスト圧縮手段は実質 cold storage 移行です）。

!!! warning "cold storage の最低保持期間"
    AWS Backup の cold storage は最低 90 日課金されます。このため `cold_storage_after_days` を使う階層では `delete_after_days` が `cold_storage_after_days + 90` 以上である必要があり、違反する設定は pocket.toml の検証で弾かれます（例: `cold_storage_after_days = 35` なら `delete_after_days` は 125 以上）。

!!! tip "短いサイクルで検証したい stage（sandbox 等）"
    上の 90 日制約は cold storage へ移動する階層にのみ効きます。`cold_storage_after_days = 0`（移動しない）にすれば `delete_after_days` を自由に短くできます。

    ```toml
    [sandbox.backup.dsql.daily]
    delete_after_days = 7          # 7 日で失効
    [sandbox.backup.dsql.weekly]
    cold_storage_after_days = 0
    delete_after_days = 14
    [sandbox.backup.dsql.monthly]
    cold_storage_after_days = 0
    delete_after_days = 31
    ```

!!! warning "destroy とバックアップデータ"
    `pocket destroy` はバックアップ**設定**（backup plan / selection）を削除しますが、バックアップ**データ**（vault と recovery point）は既定では削除しません。クラスターを消した後こそ復元が必要になりうるためです。データが残る場合は destroy が件数を表示して警告します（保持期限までは課金対象です）。

    データも消したい場合は `[backup]` に `deletable = true` を宣言します。destroy 実行中の確認プロンプト（`[y/N]`、既定 No）で yes と答えた場合のみ削除されます。`--yes` フラグによる一括承認ではデータ削除は行いません（データ削除だけは暗黙に通しません）。destroy とは別に、`pocket backup cleanup` でデータだけを削除することもできます（[CLI リファレンス](cli.md) 参照）。

!!! info "宣言を外した場合"
    `[backup.dsql]` 等を後から外しても、deploy は既存の plan（スケジュール）に触りません（snapshot は取られ続けます）。スケジュールを止めるには destroy を実行するか、AWS Backup コンソールから plan を削除してください。

!!! info "権限が足りない場合"
    deploy role に `backup:*`（[権限リファレンス](../permissions/aws.md) 参照）が無い場合、deploy は失敗せず警告を出して定期バックアップの provisioning をスキップします。警告が出たら権限を付与してください。

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

## container

AWS Lambda コンテナの設定です。`[container.<name>]` の dict 形式で、**1 プロジェクトに
複数の container を宣言できます**（0.29.0 で旧 `[awscontainer]`（単数）から
リネーム・一般化。移行手順は CHANGELOG 0.29.0 を参照）。

```toml
[container.main]
dockerfile_path = "pocket.Dockerfile"
```

`<name>` は英小文字始まりの英小文字 + 数字（最大 32 文字、hyphen 不可）です。
Lambda 関数名 (`{prefix}{name}-{handler}`) や ECR リポジトリ名
(`{prefix}{name}-lambda`)、container stack 名 (`{slug}-container-{name}`) の
slot として使われます。

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `dockerfile_path` | str | **必須** | Dockerfileのパス |
| `platform` | str | `"linux/amd64"` | Dockerビルドプラットフォーム（`"linux/amd64"` / `"linux/arm64"`）。Lambda の `Architectures` もこの値から導出される |
| `envs` | dict[str, str] | `{}` | Lambda環境変数 |
| `use_vpc` | bool \| None | None | VPC利用の制御（[use_vpc](#use_vpccontainer--rds) 参照） |
| `ecr_name` | str \| None | None | ECRリポジトリ名の上書き。省略時は `{stage}-{project}-{namespace}-{name}-lambda` |
| `build` | str \| dict | `"codebuild"` | コンテナイメージのビルドバックエンド（下記参照） |
| `permissions_boundary` | str \| None | None | Lambda 実行ロール / CodeBuild ロールに適用する IAM Permissions Boundary の ARN（[IAM 権限](../permissions/aws.md) 参照） |

### 複数 container（strangler 移行 / 異 runtime 並行稼働）

同一 CloudFront distribution の path 単位で別 runtime の Lambda origin を足す
（例: Django を残したまま `/v2/*` だけ Rust 実装に切り替える）には、container を
複数宣言し、cloudfront routes の `handler` を `"<container>.<handler>"` の
ドット記法で参照します。

```toml
[container.mydjango]
dockerfile_path = "Dockerfile"
[container.mydjango.handlers.wsgi]
command = "pocket.django.lambda_handlers.wsgi_handler"
apigateway = {}

[container.v2]
dockerfile_path = "v2/Dockerfile"
[container.v2.handlers.wsgi]
command = "admin-v2"          # Rust バイナリ名
apigateway = {}

[cloudfront.web]
routes = [
    { path_pattern = "/v2/*", type = "lambda", handler = "v2.wsgi" },
    { type = "lambda", handler = "mydjango.wsgi", is_default = true },
]
```

- handler 参照（cloudfront routes / scheduler）は container が 1 つでも常に
  ドット記法です
- 各 Lambda には `POCKET_CONTAINER=<name>` が注入され、runtime は自分の
  `[container.<name>]` を自動選択します
- CLI の container 単位コマンド（`pocket resource container ...` /
  `pocket resource image ...`）は `--container <name>` で対象を指定します
  （container が 1 つだけなら省略可）
- 自 container の handler は従来どおり `POCKET_<HANDLER>_HOST` 等で参照でき、
  他 container の handler は `POCKET_<CONTAINER>_<HANDLER>_HOST` /
  `_ENDPOINT` / `_QUEUEURL` の修飾名で参照します
- managed secret は既定で **container ごとに独立**です（保存先は
  `{stage}-{project}-{name}-{namespace}` の container store で、同名の宣言も
  container ごとに別の値が生成されます）。複数 container で値を共有したい
  場合は全宣言に `shared = true` を付けます（保存先は
  `{stage}-{project}-{namespace}` の shared store。strangler 移行で Django の
  `SECRET_KEY` を新旧 container が共有する用途）。
  詳細は [container.secrets](#containersecrets) を参照

### build（ビルドバックエンド）

コンテナイメージのビルド方法を指定します。文字列で backend のみ指定するショートハンドと、テーブルでの詳細指定の両方が使えます。

```toml
[container.main]
build = "docker"   # ショートハンド

# または詳細指定
[container.main.build]
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
    [container.main]
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

!!! warning "ビルドコンテキストのファイル permission"
    Lambda の実行ユーザーは非 root のため、other-read の無いファイル（編集操作の
    副作用で生まれる mode 600 等）が image にそのまま COPY されると読めず、
    **全 handler が INIT フェーズで失敗します**（wsgi も同じ image のためサイトごと
    500。表面のエラーは `Runtime.Unknown` で原因が分かりにくい）。

    - `backend = "codebuild"` はソース zip 作成時に mode を 0644/0755 へ自動正規化
      します
    - `backend = "docker"` / `"depot"` は生の permission のまま image に入るため、
      build 前にコンテキストを走査して該当ファイルを警告します。ただし runtime が
      INIT フェーズで読む `pocket.toml` / `pocket.runtime.toml` は該当 container が
      確実に INIT 失敗するため、警告でなく**エラーで deploy を中断**します
      (0.31.0 から。`chmod 644` で修正、image に COPY しないなら .dockerignore へ)
    - 自前の Dockerfile では COPY に `--chmod` を付けて build 段で正規化するのが
      確実です（`pocket django init` 生成のテンプレートは適用済み）:

    ```dockerfile
    # --chmod はディレクトリにも同じ mode が付くため、traverse に x が必要な 755 を使う
    COPY --chmod=755 . .
    ```

### pocket runtime-config

`pocket.toml` からビルド専用の設定（`dockerfile_path`, `managed_assets`, `build`, `upload_dir` 等）を除外した TOML を生成します。

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
    | `pocket.runtime.toml` | `pocket.toml` の runtime 用 sanitized 版。`container.main.django.project_dir` が設定されていれば `{project_dir}/pocket.runtime.toml` に出力 |
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

### container.handlers

Lambda関数の設定を記述します。キー名がハンドラー名になります。

```toml
[container.main.handlers.wsgi]
command = "pocket.django.lambda_handlers.wsgi_handler"

[container.main.handlers.management]
command = "pocket.django.lambda_handlers.management_command_handler"
timeout = 600
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `command` | str | **必須** | Lambda コンテナの `ImageConfig.Command`（エントリーポイント） |
| `timeout` | int | `30` | タイムアウト（秒） |
| `memory_size` | int | `512` | メモリサイズ（MB） |
| `reserved_concurrency` | int \| None | None | 予約済み同時実行数 |
| `envs` | dict[str, str] | `{}` | handler 単位の環境変数。[container.main].envs とマージされ handler 側が優先 |

`envs` を使うと、同一イメージ・同一バイナリを環境変数でモード切替して複数の Lambda に並べられます（Rust バイナリなど、Django の management ハンドラーのようなモジュールパス切替が使えない場合に有用です）:

```toml
[container.main.handlers.web]
command = "myapp-lambda"

[container.main.handlers.admin]
command = "myapp-lambda"
timeout = 600
envs = { MYAPP_MODE = "admin" }
```

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
[dev.container.main.handlers.wsgi]
apigateway = {}

# 独自ドメインを利用する場合
[prod.container.main.handlers.wsgi]
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
[container.main.handlers.sqsmanagement]
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

### container.secrets

シークレット管理の設定です。保存先として Secrets Manager (`sm`) と SSM Parameter Store (`ssm`) を選択できます。

```toml
[container.main.secrets]
store = "sm"  # "sm" (Secrets Manager) or "ssm" (SSM Parameter Store)
pocket_key_format = "{stage}-{project}-{namespace}"
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `store` | `"sm"` \| `"ssm"` | `"sm"` | シークレットの保存先 |
| `pocket_key_format` | str | `"{stage}-{project}-{namespace}"` | シークレットキーのフォーマット |
| `require_list_secrets` | bool | `false` | ListSecrets権限を付与 |

`store` と `pocket_key_format` は全 container で一致が必要です（複数 container
構成でも保存の起点を 1 つに保つため）。

!!! info "保存先パス: container store と shared store"
    managed secret の保存先は宣言によって 2 種類に分かれます。

    | 宣言 | 保存先 (pocket_key) | 意味 |
    |------|--------------------|------|
    | 既定 (`shared` なし) | `{stage}-{project}-{name}-{namespace}` | container ごとに独立した値。SM コンソール上も container 名で識別できる |
    | `shared = true` | `{stage}-{project}-{namespace}` | 同名 + 同 spec で宣言した全 container が同じ値を共有 |

    user secret (stored mode) の正準パスと dsql endpoint の publish 先は常に
    project 側 (`{pocket_key}-user/...`) です。DB URL 等は stage 単位の外部資源への
    参照であり、container ごとに分ける対象ではないためです。

#### secrets.managed

magic-pocketが自動生成・管理するシークレットを定義します。

```toml
[container.main.secrets.managed]
SECRET_KEY = { type = "password", options = { length = 50 } }
DJANGO_SUPERUSER_PASSWORD = { type = "password", options = { length = 16 } }
DATABASE_URL = { type = "auto_database_url" }
```

各 spec は `shared = true` を付けると shared store (project 共有パス) に保存され、
同名 + 同 spec で宣言した複数 container が同じ値を共有します。`shared` なしの
同名宣言 (複数 container) はそれぞれの container store に独立した値として
生成されます (spec が違ってもかまいません)。`shared` の有無が混在する同名宣言も
可能で、`shared` を付けた container 同士だけが値を共有し、無印の container は
独立した値を持ちます (「2 container で共有 + 1 container は独立」のような構成)。
ただし cloudfront から key 名で参照される secret
(`token_secret` / `basic_auth` / `signing_key`) は値の候補が 1 つに決まる必要が
あるため、その key に限り「無印の複数宣言」や「shared と無印の混在」はエラーに
なります。

```toml
# strangler 移行: 新旧 container で Django の署名 secret を共有する
[container.mydjango.secrets.managed]
SECRET_KEY = { type = "password", options = { length = 50 }, shared = true }

[container.v2.secrets.managed]
SECRET_KEY = { type = "password", options = { length = 50 }, shared = true }
```

**type = "auto_database_url"**
:   pocket.toml 内の DB 設定（`[neon]` / `[rds]` / `[tidb]`）を自動検出し、適切な DATABASE_URL を生成します。
    DB が1つだけ定義されている場合はそれを使用し、複数定義されている場合はエラーになります。
    ステージごとに DB を切り替える場合に便利です。オプションはありません。

    ```toml
    [container.main.secrets.managed]
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
    [container.main.secrets.managed.JWT_RSA]
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
    [container.main.secrets.managed.CF_MEDIA_KEY]
    type = "cloudfront_signing_key"
    options = { pem_base64_environ_suffix = "_PEM_BASE64", pub_base64_environ_suffix = "_PUB_BASE64", id_environ_suffix = "_ID" }
    ```
    → 環境変数 `CF_MEDIA_KEY_PEM_BASE64`, `CF_MEDIA_KEY_PUB_BASE64` が secrets 経由で、`CF_MEDIA_KEY_ID` が CloudFormation ImportValue 経由で登録されます。

**type = "spa_token_secret"**
:   SPA トークン認証用の HMAC-SHA256 シークレット（256-bit hex 文字列）を自動生成します。
    CloudFront の `token_secret` で参照し、`require_token` ルートのトークン検証に使用されます。
    オプションはありません。

    ```toml
    [container.main.secrets.managed]
    SPA_TOKEN_SECRET = { type = "spa_token_secret" }
    ```
    → 環境変数 `SPA_TOKEN_SECRET` が secrets 経由で登録されます。Django 側で `pocket.django.spa_auth` を使ってトークン生成・検証が可能です。

**type = "basic_auth_credential"**
:   CloudFront の Basic 認証用 credential（`user:pass` 形式）を生成します。
    CloudFront の [`basic_auth`](#cloudfrontbasic_auth) で参照します。
    `username` は必須、`password` は省略時に英数字ランダム（`length`、既定 16 文字）で生成されます。

    ```toml
    [container.main.secrets.managed]
    BASIC_AUTH = { type = "basic_auth_credential", options = { username = "preview" } }
    # password を固定したい場合 (値が pocket.toml = git に入る点に注意):
    # BASIC_AUTH = { type = "basic_auth_credential", options = { username = "preview", password = "shared-pass" } }
    ```
    → 生成値は `pocket resource container secrets list --show-values` で確認できます。

#### secrets.user

自分で作成したシークレットを参照する場合に使います。
指定すると、GetSecretValue / GetParameter 権限が自動付与されます。

```toml
[container.main.secrets.user]
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
[container.main.secrets.user]
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

[container.main.secrets.user]
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
- 導出名は **`type` 基準**（`/{pocket_key}-user/{type}`）で、env var 名（このテーブルの
  キー）には依存しません。env var をリネームしても保存先は動かず、同一 `type` の user
  secret は 1 stage につき 1 個までです。この規約は 0.12.0 で導入されました。0.11 以前で
  provision 済みの環境は、`pocket migrate secret-paths --stage <stage>`（または引数なしの
  `pocket migrate`）で旧パス（キー基準）から新パス（type 基準）へ移設できます（冪等）。
- **`pocket resource <db> url --stage <stage>`** は保存済み URL を stdout に出力します
  （移行ツール等が `$(...)` で食える純テキスト）。`type` 基準で解決するため、consumer の
  `DATABASE_URL` が別 backend を指していても「その backend の保存 URL」を引けます。

#### secrets.extra_resources

追加のシークレットARN（正規表現可）に対してGetSecretValue / GetParameter 権限を付与します。

```toml
[container.main.secrets]
extra_resources = ["arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:my-prefix-*"]
```

#### secrets の即時反映 (`pocket resource container reload-env`)

SSM / Secrets Manager 側でシークレット値を更新しても、**warm container は
旧値を抱えたまま再利用される**ため、新値の反映は次の cold start を待つ
形になります (典型的には 5〜15 分のラグ)。feature flag の即時切替、secret
rotation 後の即時反映、hotfix で env 1 つだけ変えたい等のユースケースで
このラグが許容できない場合は、`pocket resource container reload-env` を使います。

```bash
# 全 handler の env を SSM/SM の最新値で再構築 + 即時反映
pocket resource container reload-env --stage=prod

# 特定 handler のみ
pocket resource container reload-env --stage=prod --handler=wsgi

# 現状確認 (Lambda 側の env と SSM/SM の宣言値が drift してないか)
pocket resource container status-env --stage=prod
```

仕組み:

1. pocket.toml の `[container.main.secrets.managed/user]` から「現在の宣言上の
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

!!! note "secrets 以外の env (POCKET_STAGE / container.envs / RDS pointers 等)"
    `reload-env` は **secrets のキーだけ更新** し、その他の env は Lambda の
    現状値を保持します。POCKET_STAGE 等の静的 env を変更したい場合は通常の
    `pocket deploy` を使ってください。

### container.iam

Lambda execution role に追加で IAM 権限を注入します。`use_s3` / `use_route53` / `secrets.allowed_*_resources` 等の built-in な仕組みでカバーできない権限を、ユーザーが宣言的に与えるための逃げ道です。

```toml
[container.main.iam]
managed_policy_arns = [
    "arn:aws:iam::aws:policy/AdministratorAccess",
]

[container.main.iam.inline_policies.cross-account-assume]
Version = "2012-10-17"

[[container.main.iam.inline_policies.cross-account-assume.Statement]]
Effect = "Allow"
Action = "sts:AssumeRole"
Resource = "arn:aws:iam::*:role/provisioner-role"
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|------|----------|------|
| `managed_policy_arns` | list[str] | `[]` | LambdaRole の ManagedPolicyArns に追加する AWS managed policy ARN の list |
| `inline_policies` | dict[str, dict] | `{}` | LambdaRole の Policies に追加する inline policy。key は PolicyName の suffix (`resource_prefix` が前置される)、value は PolicyDocument の dict |

inline_policies の value は標準的な IAM PolicyDocument の形式 (`Version` / `Statement` を含む dict) です。TOML の制約から `Statement` を複数行で書く場合は `[[container.main.iam.inline_policies.<name>.Statement]]` 形式の table array を使います。

!!! warning "宣言的な仕組みでカバーできない場合の最終手段"
    まずは `use_s3` / `use_route53` / `use_ses` / `use_sqs` 等の service flag や `[container.main.secrets]` の `allowed_sm_resources` / `allowed_ssm_resources` で対応できないかを検討してください。
    `container.<name>.iam` は admin tool 等で広い権限が必要なケース、もしくは magic-pocket が built-in でサポートしていない AWS サービスへの権限が必要な場合の逃げ道です。

### container.django

Lambda環境で利用するDjango設定を記述します。

#### storages

Djangoの `STORAGES` に設定する内容です。

```toml
[container.main.django.storages]
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
| `link` | bool | `false` | collectstatic を `--link` で実行（`static=true` 時のみ）。下記参照 |

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
    [container.main.django.storages]
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
    [container.main.django.storages]
    staticfiles = { store = "s3", location = "static", static = true, publish = "command" }
    ```

!!! note "link — collectstatic を symlink で行う"
    `link = true` を宣言すると、`pocket django deploy` / `promote` /
    `deploystatic` のすべての経路で collectstatic に `--link` が付きます。
    ビルド先が全量 symlink（0 バイト）になり、大容量資産の複製コストが
    かかりません。`aws s3 sync` は symlink を追うためアップロードは
    従来と互換です。CLI の `pocket django deploystatic --link/--no-link`
    フラグは宣言の上書き用に使えます。

    link 有効時はビルド先（`pocket_cache/static_build/<stage>/`）を
    collectstatic の前に毎回クリアします。非 link で作られた実体ファイルが
    混在すると collectstatic が全ファイルを実体コピーで作り直すこと、
    ソースファイル削除後に壊れた symlink が残ると `aws s3 sync` が失敗する
    ことを避けるためです（symlink の再作成は安価なのでクリアのコストは
    無視できます）。非 link 時は従来どおりクリアしません。

    ```toml
    [container.main.django.storages]
    staticfiles = { store = "s3", location = "static", static = true, link = true }
    ```

#### caches

Djangoの `CACHES` に設定する内容です。

```toml
[container.main.django.caches]
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
[dev.container.main.django.settings]
DEFAULT_FROM_EMAIL = '"Dev" <test@example.com>'
CORS_ALLOWED_ORIGINS = ["https://dev.example.com"]

[prod.container.main.django.settings]
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
| `basic_auth` | str \| None | None | distribution 全体の Basic 認証用 managed secret 名（`type = "basic_auth_credential"`。下記 [basic_auth](#cloudfrontbasic_auth) 参照） |
| `managed_assets` | str \| None | None | ステージ別アセットのディレクトリ（下記参照） |
| `waf` | dict \| None | None | WAFv2 IP allowlist を attach（下記 [waf](#waf) 参照） |
| `enable_origin_verify` | bool | `false` | origin 直叩き防止 + 詐称耐性 client IP（下記 [origin verify](#origin-verify-enable_origin_verify) 参照） |

### cloudfront.basic_auth

一般公開前の sandbox / stg サイト全体を Basic 認証で隠せます。distribution 単位の設定で、S3 / SPA / lambda を含む**全 behavior** に適用されます。

```toml
[container.main.secrets.managed]
BASIC_AUTH = { type = "basic_auth_credential", options = { username = "preview" } }

[sandbox.cloudfront.web]
domain = "www.sandbox.example.com"
basic_auth = "BASIC_AUTH"   # managed secret のキー名を参照 (token_secret と同じ方式)
```

- 検証は viewer-request の CloudFront Function が行い、期待する `Authorization` ヘッダ値（`Basic <base64>`）を KVS から読んで文字列比較します。不一致は 401 + `WWW-Authenticate` を返します
- credential は deploy 時に KVS へ書き込まれるため、rotation（secret の再生成 → 再 deploy）にスタック更新は不要です
- `redirect_from` や SPA トークン認証（`require_token`）とは併用可能です（各 Function に認証処理が合成されます）

!!! warning "Authorization ヘッダの占有"
    Basic 認証は `Authorization` ヘッダを使うため、アプリ自身が Authorization ヘッダ認証
    （Bearer トークン等）を使う API とは併用できません。cookie / session 認証
    （Django admin、SPA トークン認証の cookie を含む）は問題ありません。
    また cross-origin の API クライアントは preflight で弾かれます。
    「開発中のサイトを隠す」用途に限定してください。

!!! note "container.main.secrets が必要"
    credential の置き場所として managed secrets を使うため、`[container.main.secrets]`
    の宣言が必要です（静的サイトのみの構成でも同様）。

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
    managed_assets は S3 の `pocket_managed/` プレフィックスに配置されるため、SPA の `build` / `upload_dir` アップロードとは独立しています。`--delete` による意図しない削除の心配はありません。

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
| `allow_rules` | list | `[]` | 他ルールより先に評価される allow（下記 [allow_rules](#allow_rules) 参照） |

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

#### allow_rules

IP allowlist で閉じた stage でも、外形 smoke・uptime チェックなど特定の経路
だけを宣言的に開けられます。`allow_rules` は **IPSet / managed rules より先に
評価される allow** で、match したら通し、しなければ従来どおりの判定に落ちます。

```toml
[prod.cloudfront.web.waf]
enable_ip_set = true

# (a) path 素通し: IP に関係なく誰でも通す (公開しても実害が無いもの向け)
[[prod.cloudfront.web.waf.allow_rules]]
path = "/api/health"

# (b) secret header: pocket が managed secret を自動生成し、
#     固定ヘッダ x-pocket-waf-allow に同値を載せた呼び手だけ通す
[[prod.cloudfront.web.waf.allow_rules]]
path = "/api/smoke/*"
header = "SMOKE_ALLOW_SECRET"
```

| フィールド | 型 | 説明 |
|-----------|------|------|
| `path` | str \| None | 対象 path。末尾 `*` は prefix 一致、それ以外は完全一致 |
| `header` | str \| None | managed secret のキー名。指定するとヘッダ `x-pocket-waf-allow` の値が secret と一致するリクエストのみ allow |

- `path` / `header` の**少なくとも一方が必要**です。両方指定した場合は AND
  (その path かつ secret 持ちのみ allow) になります
- `header` の secret は **toml に値を書かず**、pocket が自動生成して
  SSM に保存します (`enable_origin_verify` の `POCKET_ORIGIN_VERIFY_SECRET` と
  同じ managed secret 経路)。ヘッダ名は `x-pocket-waf-allow` 固定です
- `header` を使う場合は `[container.main]` が必要です (secret の保存先のため)
- allow_rules は **WAF を弱める宣言**なので、deploy のたびに一覧が表示されます。
  `pocket settings --stage=<stage>` の出力でも確認できます

CI (GitHub Actions 等) から secret header 付きで叩く例:

```bash
# store = "ssm" の場合 (パラメータ名は /{pocket_key}/<KEY>)
SECRET=$(aws ssm get-parameter \
  --name "/{stage}-{project}-pocket/SMOKE_ALLOW_SECRET" \
  --with-decryption --query Parameter.Value --output text)
curl -H "x-pocket-waf-allow: $SECRET" https://example.com/api/smoke/health
```

!!! note "managed secret は Lambda の設定 env には現れません"
    managed secret は Lambda の「設定」(GetFunctionConfiguration で見える env)
    には焼き込まれず、**runtime 起動時に SSM/SM から注入**されます。CI 等の
    外部から値を読む場合は Lambda の設定ではなく secret store
    (SSM: `/{stage}-{project}-pocket/<KEY>`) を参照してください。

!!! note "secret 値は WAF ルールに焼き込まれます"
    header rule の期待値は WebACL の rule 定義 (CFn template) に含まれます。
    `cloudformation:GetTemplate` / `wafv2:GetWebACL` が可能な principal からは
    値が見える点は `enable_origin_verify` の origin custom header と同じ
    exposure class です。

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
    { type = "lambda", handler = "main.wsgi", is_default = true },
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

#### axum (Rust) middleware の組み込み

Rust container には pocket-rs 同梱の axum middleware (feature `axum`) を使います。
挙動は Django 版と同じで、secret の env 注入は `set_envs()` が自動で行います。
組み込み方は「[Rust 連携](loco.md#origin-verify-middleware)」を参照してください。
    rotate 直後は、新 header を受け取る warm Lambda がまだ旧 env を保持する一瞬の窓で
    403 になり得ます (cold start で解消)。無停止 rotation が必要な場合は別途検討します。

### redirect_from

```toml
[prod.cloudfront.main]
domain = "www.example.com"
redirect_from = [{ domain = "example.com" }]
```

`redirect_from` に指定したドメインは、その CloudFront ディストリビューションの
別名（Alias）として同じ配信に載り、証明書も `domain` の証明書に SAN として
まとめられます。リクエストは viewer-request の CloudFront Function が判定し、
canonical な `domain` へ **301 (path・query 保持)** でリダイレクトします
（専用のリダイレクト用ディストリビューションや S3 バケットは作りません）。

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
| `origin_path` | str \| None | None | S3 オリジンパス（`type = "lambda"` では指定不可）。`path_pattern` が prefix を持つ route（例 `/media/*`）では**省略可**（省略時は S3 key が単一 prefix `media/` になる）。catch-all（`path_pattern = ""` / `"/*"`）では**必須** |
| `path_pattern` | str | `""` | パスパターン |
| `is_default` | bool | `false` | CloudFront の DefaultCacheBehavior として使用 |
| `is_spa` | bool | `false` | SPA用の設定（フォールバックHTML対応） |
| `versioning` | `"content_hash"` \| `"deploy_hash"` \| None | None | バージョニング方式。`content_hash` = ファイル内容ハッシュ (ManifestStaticFilesStorage)、`deploy_hash` = git hash で URL prefix 付与 |
| `spa_fallback_html` | str | `"index.html"` | SPAフォールバック先のHTML |
| `versioned_max_age` | int | `31536000` | バージョン付きアセットのmax-age（秒、デフォルト1年） |
| `ref` | str | `""` | ルートの参照名（Django storage の route で参照） |
| `signed` | bool | `false` | 署名付きURL（distribution に `signing_key` が必要） |
| `build` | `{ dir, cmd }` \| None | None | pocket にビルドさせる宣言。`dir`（成果物ディレクトリ = アップロード対象）と `cmd`（ビルドコマンド、deploy が upload 前に shell 実行）は**両方必須** |
| `upload_dir` | str \| None | None | ビルドは外部（CI 等）の責任と宣言し、このディレクトリの中身をアップロードだけする。**deploy はビルドを実行しない**ため、成果物を最新にするのは利用者の責任 |
| `require_token` | bool | `false` | SPA トークン認証を有効化（`is_spa = true` 必須） |
| `login_path` | str | `"/api/auth/login"` | 未認証時のリダイレクト先パス |

!!! note "制約"
    - `routes` には `is_default = true` のルートが1つ必要です。
    - `is_default = true` のルートは `path_pattern` を空にする必要があります。
    - `is_spa` と `versioning` は同時に設定できません。
    - `path_pattern` は空でないルートは `/` で始まる必要があります。
    - `signed = true` のルートには、distribution に `signing_key` の設定が必要です。
    - `type = "lambda"` のルートでは `origin_path`, `is_spa`, `versioning`, `signed`, `require_token`, `build`, `upload_dir` は使用できません。`is_default = true` は許可されており、Django 単体構成（全リクエストを API Gateway に流す）で利用できます。
    - `origin_path` は `/` で始まり `/` で終わらない必要があります。バケット直下を配信する `origin_path = "/"` はサポートしません（後述の warning を参照）。
    - 旧 `type = "api"` は廃止されました。`type = "lambda"` を使ってください（起動時に分かりやすいエラーが出ます）。
    - 旧 `is_versioned` は廃止されました。`versioning = "content_hash"` を使ってください。
    - `handler` は `container.main.handlers` に定義されている必要があり、`apigateway` が設定されていなければなりません。
    - `build` と `upload_dir` は同時に設定できません（ビルド責任の宣言はどちらか一方）。
    - 旧 `build_dir` と旧文字列形式の `build = "..."` は廃止されました。`build = { dir = "...", cmd = "..." }` または `upload_dir = "..."` を使ってください（起動時に移行手順つきのエラーが出ます）。
    - `require_token = true` のルートには `is_spa = true` が必須です。distribution に `token_secret` の設定が必要です。

!!! note "差分アップロードと invalidation の範囲"
    `build` / `upload_dir` のアップロードは差分のみです。S3 上のオブジェクトと内容が
    一致するファイルはスキップされるため、数千ファイル規模のアセット（アイコン集・
    絵文字画像・フォント等）を `upload_dir` に置いても deploy 時間は伸びません。

    - 判定は **ETag**。単一 PUT でアップロードされたオブジェクトの ETag は中身の MD5
      なので、ローカルの MD5 と一致したときだけスキップします
    - **multipart でアップロードされたオブジェクト（ETag が `<hash>-<パート数>` 形式）と、
      ETag が MD5 にならないオブジェクト（SSE-KMS 等）は常に再アップロード**します。
      内容の同一性を断定できないためで、サイズ比較での代替はしません（同じサイズで
      内容が違うファイルを「変更なし」と誤判定すると、以後どの deploy でもそのファイルが
      更新されなくなります）
    - ローカルに存在しない key は従来どおり削除されます
    - CloudFront の invalidation は**変更があったルートの `path_pattern` に限定**され、
      変更が無ければ invalidation 自体を発行しません。配信専用ルート（`upload_dir` を
      持たないルート）や `versioning = "content_hash"` の immutable なアセットが
      巻き添えで無効化されることはありません

!!! tip "S3 key の二重 prefix を避ける（`origin_path` 省略）"
    S3 route の S3 key prefix は `origin_path + path_pattern` で計算されます。CloudFront の
    `origin_path` はリクエスト URI の前に付加されるため、`path_pattern = "/media/*"` の route に
    `origin_path = "/media"` を付けると、S3 の実キーは `media/media/...` と**二重階層**になります。

    `path_pattern` が prefix を持つ route（`/media/*` 等）は **`origin_path` を省略**すると、
    S3 key が `path_pattern` 由来の**単一 prefix**（`media/...`）になります。`aws s3 sync` 等で
    バケットを直接操作する運用で prefix が直感的になります。

    ```toml
    routes = [
        { is_default = true, is_spa = true, origin_path = "/spa" },  # catch-all は必須
        { path_pattern = "/static/*", ref = "static" },              # origin_path 省略 → static/
        { path_pattern = "/media/*", ref = "media" },                # origin_path 省略 → media/
    ]
    ```

!!! warning "バケット直下の配信（`origin_path = "/"`）はサポートしません"
    catch-all の route（`path_pattern = ""` / `"/*"`）に `origin_path = "/"` を指定して
    **バケット直下をそのまま配信することはできません**。`origin_path` は先頭 `/` 始まり・
    末尾 `/` なしが必須なので `"/"` は設定エラーになります。意図的な制約で、緩和する予定は
    ありません。

    理由は、pocket が **1 つの S3 バケットを複数の route で共有する**設計だからです
    （`/static` は Django の静的ファイル、`/media` はアップロード、`/spa` はフロントエンド、
    `/pocket_managed` は pocket 自身の管理ファイル…）。catch-all をバケット直下に向けると:

    - **CloudFront に渡す OAC バケットポリシーがバケット全体（`arn:aws:s3:::<bucket>/*`）
      許可になります。** 現在は route ごとの prefix から最小の共通 prefix を計算して
      許可範囲を絞っています。バケット全体を許可すると、route を張っていない prefix の
      オブジェクトまで CDN 経由で到達可能になり得ます。
    - **他の route の prefix と衝突します。** catch-all が撒くオブジェクトがバケット直下に
      散らばり、`static/` や `media/` と同じ階層に混ざります。

    catch-all に prefix を与えれば同じ配信結果が得られ、上記の問題も起きません。
    バケット直下に置きたかった場合も、`origin_path` を 1 つ足すだけで機能的な違いは
    ありません（URL 上のパスは `path_pattern` で決まり、`origin_path` は S3 側の
    key prefix にしか影響しないため）。

    ```toml
    routes = [
        # NG: origin_path = "/" は設定エラー。catch-all で origin_path 省略も同様
        { is_default = true, is_spa = true, origin_path = "/spa" },  # OK
    ]
    ```

    catch-all（`path_pattern = ""` / `"/*"`）は prefix を持たないため `origin_path` は必須のままです
    （省略するとバケット直下に散らばり他 route と衝突するため）。

    !!! warning "既存デプロイの移行"
        既にデプロイ済みの route から `origin_path` を外すと S3 key prefix が変わり、
        **既存オブジェクトが参照できなくなります**（`media/media/` → `media/`）。単一 prefix へ
        移行する場合は、CloudFront が読む新 prefix へ既存オブジェクトを `aws s3 mv` 等で移送して
        ください。新規 route はそのまま省略で構いません。

### Django 単体構成（CloudFront → Lambda のみ）

SPA を持たず、Django テンプレートで完結するプロジェクトを CloudFront 経由で配信する構成です。
`is_default = true` の `type = "lambda"` ルートを 1 つだけ定義します。

```toml
[container.main.handlers.wsgi]
command = "pocket.django.lambda_handlers.wsgi_handler"
apigateway = {}

[prod.cloudfront.web]
domain = "www.example.com"
routes = [
    { is_default = true, type = "lambda", handler = "main.wsgi" },
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
    { path_pattern = "/static/*", ref = "static", versioning = "content_hash" },
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

    [container.main]
    dockerfile_path = "pocket.Dockerfile"

    [container.main.handlers.wsgi]
    command = "pocket.django.lambda_handlers.wsgi_handler"
    apigateway = {}

    [container.main.handlers.management]
    command = "pocket.django.lambda_handlers.management_command_handler"
    timeout = 600

    [container.main.django.storages]
    default = { store = "filesystem" }
    staticfiles = { store = "s3", static = true, distribution = "web", route = "static" }

    [container.main.secrets]
    store = "ssm"

    [container.main.secrets.managed]
    SECRET_KEY = { type = "password", options = { length = 50 } }

    [sandbox.cloudfront.web]
    domain = "sandbox.myproject.example.com"
    routes = [
        { type = "lambda", handler = "main.wsgi", is_default = true },
        { path_pattern = "/static/*", ref = "static", versioning = "deploy_hash" },
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
[container.main.handlers.wsgi]
command = "pocket.django.lambda_handlers.wsgi_handler"
apigateway = {}

[cloudfront.main]
domain = "example.com"
routes = [
    { is_default = true, is_spa = true },
    { path_pattern = "/api/*", type = "lambda", handler = "main.wsgi" },
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
[container.main.secrets.managed]
SECRET_KEY = { type = "password", options = { length = 50 } }
SPA_TOKEN_SECRET = { type = "spa_token_secret" }

[cloudfront.main]
token_secret = "SPA_TOKEN_SECRET"
routes = [
    { is_default = true, is_spa = true, require_token = true, origin_path = "/app" },
    { path_pattern = "/api/*", type = "lambda", handler = "main.wsgi" },
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
handler = "main.worker"
input = { task = "rotate_logs" }

[scheduler.schedules.daily_digest]
scheduler = "pocket.django.management_lambda_scheduler"
cron = "0 18 * * ? *"
handler = "main.management"
manage = "send_daily_digest --verbose"
```

各 entry は `[scheduler.schedules.{key}]` の dict 形式で書きます。`{key}` (例: `rotate_logs`) はそのまま CloudFormation logical ID と物理名の素材になるため、**並び順に依存しない安定した命名**が得られます。

### entry の共通フィールド

| フィールド | 型 | デフォルト | 説明 |
|---|---|---|---|
| `scheduler` | `"pocket.lambda_scheduler"` \| `"pocket.django.management_lambda_scheduler"` \| `"pocket.sqs_scheduler"` | `pocket.lambda_scheduler` | スケジューラ実装。default は汎用 Lambda |
| `cron` | str \| None | None | EventBridge cron 式（`cron(...)` のラッパー部分は不要、中身だけ書く） |
| `rate` | str \| None | None | EventBridge rate 式（`rate(...)` のラッパー部分は不要） |
| `handler` | str | **必須** | `container.main.handlers.{key}` の key を指定 |

`cron` と `rate` は **どちらか一方を必ず指定**します（両方や両方無しはエラー）。

### `pocket.lambda_scheduler` (default)

汎用の Lambda invoke。任意の dict を `input` フィールドで EventBridge Target Input にそのまま渡します。

```toml
[scheduler.schedules.rotate_logs]
rate = "1 hour"
handler = "main.worker"
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
handler = "main.management"
manage = "send_daily_digest some_param --verbose --batch-size 100"
```

| フィールド | 型 | デフォルト | 説明 |
|---|---|---|---|
| `manage` | str | **必須** | shell-style の management command (例: `"send_daily_digest --verbose"`) |

**制約**: 参照する `handler` は `command = "pocket.django.lambda_handlers.management_command_handler"` でなければなりません（deploy 前にバリデーションエラーになります）。

実装的には、Lambda には `{"manage": "<コマンド文字列>"}` が渡され、handler 側で `shlex.split` → `call_command` を行います。既存の `{command, args, kwargs}` 形式と完全に共存しており、後方互換性は壊しません。

### `pocket.sqs_scheduler`

Lambda を直接 invoke せず、**handler の SQS queue へ `SendMessage`** します（EventBridge Scheduler の universal target。`message` が JSON 化されて MessageBody になります）。定期実行を queue に載せることで、リトライは SQS の visibility timeout / redrive に一元化され、**失敗系の監視は queue の DLQ 1 箇所だけ**になります。worker handler は SQS event だけを受ければよく、「EventBridge 直接 invoke と SQS event の両受け」を実装する必要がありません。

```toml
[container.main.handlers.sqsmanagement]
command = "pocket.django.lambda_handlers.sqs_management_command_report_failures_handler"
timeout = 60
sqs = {}

[scheduler.schedules.cleanup]
scheduler = "pocket.sqs_scheduler"
rate = "15 minutes"
handler = "main.sqsmanagement"
message = { command = "clearsessions", args = [], kwargs = {} }
```

| フィールド | 型 | デフォルト | 説明 |
|---|---|---|---|
| `message` | dict | `{}` | JSON 化されて SQS MessageBody として送られる dict |

**制約**: 参照する `handler` は `sqs` を設定している必要があります（deploy 前にバリデーションエラーになります）。

`message` の形式は受け側の worker が決めます。Django の SQS management handler (`sqs_management_command_report_failures_handler`) へ送る場合は `command` / `args` / `kwargs` の 3 キーが必須です。Rust worker の場合はアプリ側で定義した Job 型（serde タグ付き enum 等）に一致する形を書きます。

### ステージ別 schedule

dict 形式は **deep merge** が効くため、entry 単位で stage オーバーライド・追加・調整が自然に書けます。

```toml
# 全 stage 共通
[scheduler.schedules.rotate_logs]
rate = "1 hour"
handler = "main.worker"
input = { task = "rotate_logs" }

[scheduler.schedules.daily_digest]
scheduler = "pocket.django.management_lambda_scheduler"
cron = "0 18 * * ? *"
handler = "main.management"
manage = "send_daily_digest"

# sandbox では rotate_logs の頻度だけ落とす
[sandbox.scheduler.schedules.rotate_logs]
rate = "1 day"

# prod だけで動く追加 schedule
[prod.scheduler.schedules.month_end_invoice]
scheduler = "pocket.django.management_lambda_scheduler"
cron = "0 0 L * ? *"
handler = "main.management"
manage = "send_monthly_invoice"
```

### 命名のコツ

- **handler key** は「何をする Lambda か」(`management`, `worker`, `mailer`)
- **entry key** は「いつ動くか」(`nightly`, `hourly`, `month_end`, `rotate_logs`)

`cron` のような AWS 用語を key に使うと、cron 式そのものとの混同が起きやすいので避けてください。

### CloudFormation リソース構成

各 entry に対して 1 つの `AWS::Scheduler::Schedule` が出力されます。Lambda Permission は不要で、共有の `AWS::IAM::Role` (`{resource_prefix}scheduler`) が EventBridge Scheduler に対して `lambda:InvokeFunction` を許可します。`Resource` は schedule で参照されている Lambda 関数 ARN に絞り込まれます。`pocket.sqs_scheduler` の entry は Lambda ではなく対象 queue が Target になり、role には対象 queue に絞った `sqs:SendMessage` が付きます（その handler の Lambda ARN は `lambda:InvokeFunction` に含まれません）。

### wsgi handler のウォームアップは非対応

`wsgi_handler` は API Gateway proxy event 形式を期待するため、scheduler が渡す任意 input dict では呼び出せません。Lambda のコールドスタートを抑えたい場合は **Provisioned Concurrency** を利用してください（reserved_concurrency や warmup の handler を別途書くより堅牢です）。
