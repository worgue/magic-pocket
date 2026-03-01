# コマンドライン (CLI)

magic-pocketは `pocket` コマンドを提供します。
全てのデプロイ関連コマンドは `--stage` オプションで対象ステージを指定します。

以下の例では dev 環境を操作する場合を示します。

---

## POCKET_STAGE 環境変数

環境変数 `POCKET_STAGE` を設定すると、`--stage` オプションのデフォルト値として使用されます。

```bash
export POCKET_STAGE=dev

# 以下は全て --stage=dev と同等
pocket deploy
pocket status
pocket resource s3 status
```

`--stage` を明示的に指定すると、環境変数より優先されます。

```bash
export POCKET_STAGE=dev

# 環境変数を上書き
pocket deploy --stage=prd
```

`POCKET_STAGE` も `--stage` も未指定の場合は、プロンプトで入力を求められます。

!!! note "Lambda ランタイムとの共用"
    `POCKET_STAGE` はLambda上でランタイム環境の判定にも使用されています（[実行環境とDjango連携](runtime.md) を参照）。
    CLIのデフォルトステージとしても同じ環境変数を共用しているため、変数名を変える必要はありません。

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
- RDS Aurora クラスターの作成（`[rds]` 設定時）
- Secrets Managerへのシークレット登録
- S3バケットの作成と権限設定
- コンテナイメージの作成とECRへのアップロード
- CloudFormationによるLambda関連リソースの作成・更新
- CloudFormationによるCloudFront関連リソースの作成・更新
- フロントエンドのビルドとS3アップロード（`build_dir` が設定されたルートがある場合）

| オプション | 説明 |
|-----------|------|
| `--stage` | 対象ステージ |
| `--openpath` | デプロイ後にブラウザで開くパス |
| `--skip-frontend` | フロントエンドのビルド・アップロードをスキップ |

### pocket status

全リソースの状態を確認します。

```bash
pocket status --stage=dev
```

### pocket destroy

ステージの全リソースを一括削除します。

```bash
pocket destroy --stage=dev
```

| オプション | 説明 |
|-----------|------|
| `--stage` | 対象ステージ |
| `--with-secrets` | pocket管理シークレットも削除 |
| `--with-state-bucket` | ステートバケットも削除 |

削除は以下の順序（デプロイの逆順）で行われます:

1. CloudFront（CFNスタック + バケットポリシー）
2. AwsContainer（CFNスタック + ECR + secrets）
3. RDS Aurora クラスター（Final Snapshot 付き）
4. VPC（CFNスタック + EFS）
5. S3 バケット
6. TiDB クラスタ
7. Neon ブランチ
8. ステートバケット（`--with-state-bucket` 指定時のみ）

実行前に削除対象の一覧が表示され、確認プロンプトが出ます。

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

# リソース削除（CFNスタック + ECRリポジトリ）
pocket resource awscontainer destroy --stage=dev

# シークレットも含めて削除
pocket resource awscontainer destroy --stage=dev --with-secrets
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

### rds

```bash
# RDS Aurora クラスターの状態確認
pocket resource rds status --stage=dev

# 接続情報の表示（endpoint, port, database, username）
pocket resource rds endpoint --stage=dev

# クラスターの削除（確認プロンプト付き、Final Snapshot 作成）
pocket resource rds destroy --stage=dev
```

### s3

```bash
# S3バケットの状態確認
pocket resource s3 status --stage=dev

# S3バケットを中身ごと削除
pocket resource s3 destroy --stage=dev
```

### cloudfront

```bash
# CloudFrontの状態確認
pocket resource cloudfront status --stage=dev

# CloudFormation YAMLを表示
pocket resource cloudfront yaml --stage=dev

# CloudFormation YAMLの差分を確認
pocket resource cloudfront yaml-diff --stage=dev

# コンテキスト（テンプレートに渡される変数）を表示
pocket resource cloudfront context --stage=dev

# 特定のディストリビューションのみ
pocket resource cloudfront yaml --stage=dev --name=main

# フロントエンドのビルド・S3アップロード・キャッシュ無効化
pocket resource cloudfront upload --stage=dev

# ビルドをスキップしてアップロードのみ
pocket resource cloudfront upload --stage=dev --skip-build

# 特定のディストリビューションのみ
pocket resource cloudfront upload --stage=dev --name=main
```

`upload` は `build_dir` が設定されたルートに対して、ビルド → S3アップロード → CloudFrontキャッシュ無効化を実行します。
`pocket deploy` 実行時にも自動的に呼ばれます（`--skip-frontend` で抑制可能）。

### cloudfront_keys

CloudFront 署名付き URL 用の鍵リソースを管理します。`signing_key` が設定されたディストリビューションのみ対象です。

```bash
# CloudFormation YAMLを表示
pocket resource cloudfront-keys yaml --stage=dev

# CloudFormation YAMLの差分を確認
pocket resource cloudfront-keys yaml-diff --stage=dev

# 状態確認
pocket resource cloudfront-keys status --stage=dev

# 特定のディストリビューションのみ
pocket resource cloudfront-keys yaml --stage=dev --name=media
```

### vpc

```bash
# VPCの状態確認
pocket resource vpc status

# CloudFormation YAMLを表示
pocket resource vpc yaml

# CloudFormation YAMLの差分を確認
pocket resource vpc yaml-diff

# VPCを削除（CFNスタック + EFS）
pocket resource vpc destroy
```

!!! note "VPCコマンド"
    VPC は `pocket.toml` の `[vpc]` セクションから自動的に読み込まれます。
    外部 VPC（`manage = false`）の場合、`create` / `update` / `destroy` は実行できません。
    consumer がいる managed VPC は削除できません。

---

## CloudFormation テンプレートの確認

各リソースの `yaml` サブコマンドで、デプロイ時に使われる CloudFormation テンプレートを標準出力で確認できます。
AWS にアクセスせずにテンプレートの内容を確認したい場合に便利です。

```bash
# awscontainer（Lambda関連）
pocket resource awscontainer yaml --stage=dev

# cloudfront
pocket resource cloudfront yaml --stage=dev

# cloudfront_keys（署名付きURL用鍵）
pocket resource cloudfront-keys yaml --stage=dev

# vpc
pocket resource vpc yaml
```

`yaml-diff` サブコマンドでは、デプロイ済みのテンプレートとの差分を JSON で表示します。
デプロイ前に変更内容を確認する際に使います。

```bash
pocket resource cloudfront yaml-diff --stage=dev
```
