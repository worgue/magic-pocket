# コマンドライン (CLI)

magic-pocketは `pocket` コマンドを提供します。
全てのデプロイ関連コマンドは `--stage` オプションで対象ステージを指定します。

以下の例では dev 環境を操作する場合を示します。

---

## 基本コマンド

### pocket version

バージョンを表示します。

```bash
pocket version
```

### pocket deploy

全リソースをデプロイします。

```bash
pocket deploy --stage=dev
```

`pocket.toml` の設定に応じて、以下の処理が行われます。

- Neonへのデータベース作成
- Secrets Managerへのシークレット登録
- S3バケットの作成と権限設定
- コンテナイメージの作成とECRへのアップロード
- CloudFormationによるLambda関連リソースの作成・更新
- CloudFormationによるCloudFront関連リソースの作成・更新

### pocket status

全リソースの状態を確認します。

```bash
pocket status --stage=dev
```

---

## Django コマンド

### pocket django init

Djangoプロジェクトの初期設定ファイルを生成します。

```bash
pocket django init
```

以下のファイルが生成されます。

| ファイル | 説明 |
|---------|------|
| `pocket.toml` | デプロイ設定 |
| `pocket.Dockerfile` | Lambda用Dockerfile |
| `settings.py` | 環境変数対応に書き換え（django-environが必要） |
| `.env` | ローカル開発用の環境変数 |

### pocket django deploy

デプロイ + マイグレーション + 静的ファイルをまとめて実行する便利コマンドです。

```bash
pocket django deploy --stage=dev
```

| オプション | 説明 |
|-----------|------|
| `--stage` | 対象ステージ |
| `--openpath` | デプロイ後にブラウザで開くパス |
| `--force` | 確認プロンプトをスキップ |

!!! note "`pocket deploy` との違い"
    `pocket deploy` はインフラのデプロイのみ行います。
    `pocket django deploy` はインフラデプロイに加え、ローカルでの `collectstatic` + S3アップロード、Lambda上での `migrate` も対話形式で実行します。

### pocket django manage

Lambda上でDjangoマネジメントコマンドを実行します。

```bash
# マイグレーション
pocket django manage migrate --stage=dev

# 静的ファイル収集
pocket django manage collectstatic --noinput --stage=dev

# スーパーユーザー作成
pocket django manage createsuperuser --username=admin --email=admin@example.com --noinput --stage=dev
```

| オプション | 説明 |
|-----------|------|
| `--stage` | 対象ステージ |
| `--handler` | 特定のハンドラーを指定（非推奨） |
| `--timeout-seconds` | ログ表示のタイムアウト（秒） |

### pocket django deploystatic

静的ファイルのみをデプロイします（ローカルで `collectstatic` → S3にアップロード）。

```bash
pocket django deploystatic --stage=dev
```

| オプション | 説明 |
|-----------|------|
| `--stage` | 対象ステージ |
| `--skip-collectstatic` | collectstaticをスキップしてアップロードのみ実行 |

### pocket django storage upload

ローカルのファイルをデプロイ先のS3にアップロードします。

```bash
pocket django storage upload management --stage=dev
```

| オプション | 説明 |
|-----------|------|
| `--stage` | 対象ステージ |
| `--delete` | S3側の不要ファイルを削除 |
| `--dryrun` | 実行内容を表示するのみ |

ローカル側は `filesystem`、デプロイ先は `s3` で定義されている必要があります。

??? example "使い方の例"
    `pocket.toml` でローカルとリモートのストレージを対応付けます。

    ```toml
    [general.django_fallback.storages]
    management = { store = "filesystem", location = "data/management" }
    [awscontainer.django.storages]
    management = { store = "s3", location = "management" }
    ```

    ローカルの `data/management/` にファイルを置き、以下でアップロードします。

    ```bash
    pocket django storage upload management --stage=dev
    ```

    リモートでも `storages['management']` で同じファイルにアクセスできます。

---

## リソースコマンド

個別リソースの状態確認や管理を行います。

### awscontainer

```bash
# Lambda環境の状態確認
pocket resource awscontainer status --stage=dev

# wsgiエンドポイントのURLを表示
pocket resource awscontainer url --stage=dev

# CloudFormation YAMLを表示
pocket resource awscontainer yaml --stage=dev

# CloudFormation YAMLの差分を確認
pocket resource awscontainer yaml-diff --stage=dev
```

#### secrets サブコマンド

```bash
# シークレットの一覧表示
pocket resource awscontainer secrets list --stage=dev

# 値も表示
pocket resource awscontainer secrets list --stage=dev --show-values

# pocket管理シークレットの作成
pocket resource awscontainer secrets create-pocket-managed --stage=dev

# pocket管理シークレットの削除
pocket resource awscontainer secrets delete-pocket-managed --stage=dev
```

### neon

```bash
pocket resource neon status --stage=dev
```

### s3

```bash
pocket resource s3 status --stage=dev
```

### cloudfront

```bash
# CloudFrontの状態確認
pocket resource cloudfront status --stage=dev

# CloudFormation YAMLを表示
pocket resource cloudfront yaml --stage=dev

# CloudFormation YAMLの差分を確認
pocket resource cloudfront yaml-diff --stage=dev
```

### vpc

```bash
pocket resource vpc status --ref=main --stage=dev
```

!!! note "VPCコマンドの `--ref`"
    VPCコマンドでは `--stage` に加えて、`--ref` オプションで `general.vpcs` の `ref` 名を指定します。
