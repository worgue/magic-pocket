# コマンドライン (CLI)

magic-pocketは `pocket` コマンドを提供します。
全てのデプロイ関連コマンドは `--stage` オプションで対象ステージを指定します。

以下の例では dev 環境を操作する場合を示します。

---

## POCKET_DEPLOY_STAGE 環境変数

環境変数 `POCKET_DEPLOY_STAGE` を設定すると、`--stage` オプションのデフォルト値として使用されます。

```bash
export POCKET_DEPLOY_STAGE=dev

# 以下は全て --stage=dev と同等
pocket deploy
pocket status
pocket resource s3 status
```

`--stage` を明示的に指定すると、環境変数より優先されます。

```bash
export POCKET_DEPLOY_STAGE=dev

# 環境変数を上書き
pocket deploy --stage=prod
```

`POCKET_DEPLOY_STAGE` も `--stage` も未指定の場合は、プロンプトで入力を求められます。

!!! note "ランタイム用の `POCKET_STAGE` と別物です"
    Lambda 上のランタイム環境の判定には `POCKET_STAGE` という別の環境変数が使われます（[実行環境](runtime.md) を参照）。
    `POCKET_DEPLOY_STAGE` は **ローカル側でデプロイ対象を指定する用途専用** で、ローカル実行プロセスの runtime 動作には影響しません。
    これにより、ローカルで `POCKET_DEPLOY_STAGE=prod` を設定して `pocket deploy` を実行しつつ、`manage.py shell` などの runtime helper は AWS リソースを参照しない、という運用が可能です。

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

- Neonへのデータベース作成（`[neon]` 設定時）
- TiDB クラスターの作成（`[tidb]` 設定時）
- Upstash Redis データベースの作成（`[upstash]` 設定時）
- RDS Aurora クラスターの作成（`[rds]` 設定時）
- Secrets Managerへのシークレット登録
- S3バケットの作成と権限設定
- コンテナイメージの作成とECRへのアップロード
- CloudFormationによるLambda関連リソースの作成・更新
- CloudFormationによるCloudFront関連リソースの作成・更新
- フロントエンドのビルドとS3アップロード（`build` / `upload_dir` が設定されたルートがある場合。ビルドを実行するのは `build` のみ）

| オプション | 説明 |
|-----------|------|
| `--stage` | 対象ステージ |
| `--openpath` | デプロイ後にブラウザで開くパス |
| `--skip-frontend` | フロントエンドのビルド・アップロードをスキップ |
| `--yes`, `-y` | 確認プロンプトをスキップ（非対話実行用） |
| `--skip-check-existing` | neon/tidb/upstash の存在確認 API をスキップ |

### pocket promote

ビルド済みのコンテナイメージへステージを向けてデプロイします（再ビルドなし）。

```bash
pocket promote --stage=stg --commit-hash=<full-sha>
```

[`pocket django build`](#pocket-django-build) で push した `:<commit hash>` イメージに
`:<stage>` タグを付け替え、インフラ / Lambda を更新します。イメージのビルドは行いません。
指定した commit hash のイメージが ECR に存在しない場合はエラーになります（勝手にビルドしません）。

一度ビルドした同一イメージを複数ステージへ昇格させる build once 運用に使います。
詳細は「[build once と昇格](#build-once)」を参照してください。

| オプション | 説明 |
|-----------|------|
| `--stage` | 対象ステージ |
| `--commit-hash` | 昇格するイメージの git commit hash（**必須**） |
| `--openpath` | デプロイ後にブラウザで開くパス |
| `--skip-frontend` | フロントエンドのビルド・アップロードをスキップ |
| `--yes`, `-y` | 確認プロンプトをスキップ（非対話実行用） |
| `--skip-check-existing` | neon/tidb/upstash の存在確認 API をスキップ |

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
| `--without-secrets` | pocket管理シークレットを削除せずに残す |
| `--with-state-bucket` | ステートバケットも削除 |
| `--yes`, `-y` | 確認プロンプトをスキップ |

デフォルトでpocket管理シークレット（SSM / Secrets Manager）も削除されます。
残したい場合は `--without-secrets` を指定してください。

削除は以下の順序（デプロイの逆順）で行われます:

1. CloudFront（CFNスタック + バケットポリシー）+ ACM 証明書
2. AwsContainer（CFNスタック + ECR + CodeBuild + CloudWatch Logs + secrets）+ VPC（CFNスタック + EFS）
3. AWS Backup plan / selection（DSQL / RDS の cluster 削除より先。recovery point と vault は残す）
4. DSQL クラスター
5. RDS Aurora クラスター（Final Snapshot 付き）
6. CloudFront 署名鍵（`signing_key` 設定時）
7. S3 バケット
8. TiDB クラスタ
9. Upstash Redis
10. Neon ブランチ（root branch は Neon 仕様で単体削除できないため、project 内に
    他の branch がなければ project ごと削除。他の branch が残っている場合は
    巻き添えを避けるため警告してスキップ）
11. ステートバケット（`--with-state-bucket` 指定時のみ）

!!! note "ECR リポジトリの扱い"
    [`[awscontainer].ecr_name`](configuration.md#awscontainer) を明示指定している場合、
    ECR リポジトリは他ステージと共有されている可能性があるため削除されません（警告を表示してスキップ）。

!!! note "バックアップデータの扱い"
    バックアップ**データ**（recovery point）は既定では削除されず、残る場合は件数が警告表示されます。[`[backup]` の `deletable = true`](configuration.md#backup) を宣言している場合のみ、destroy 実行中に `[y/N]` で削除するか確認されます（既定 No。`--yes` による一括承認ではデータ削除は行いません）。

実行前に削除対象の一覧が表示され、確認プロンプトが出ます。

### pocket backup cleanup

ステージのバックアップデータ（AWS Backup の recovery point）を削除します。誤操作でデータを失わないため、[`[backup]` の `deletable = true`](configuration.md#backup) を宣言している場合のみ実行できます（削除する時だけ宣言するのが推奨です）。

```bash
pocket backup cleanup --stage=dev
```

| オプション | 説明 |
|-----------|------|
| `--stage` | 対象ステージ |
| `--yes`, `-y` | 確認プロンプトをスキップ |

削除対象は pocket 管理 vault（`pocket-backup`）にある、**現存する**対象 DB（dsql / managed rds）の recovery point です。plan（スケジュール）には触りません。`--vault` で利用者の vault に取ったオンデマンドバックアップは利用者の管理物とみなし削除しません。削除済み cluster の recovery point は ARN で引けないため対象外です（AWS Backup コンソールから削除してください）。

### pocket runtime-config

Lambda ランタイム用の `pocket.runtime.toml` を生成します。ビルド専用設定（`dockerfile_path`, `managed_assets`, `build`, `upload_dir` 等）が除外されます。

```bash
# 標準出力に出力
pocket runtime-config

# ファイルに出力
pocket runtime-config pocket.runtime.toml
```

詳細は「[設定ファイル - pocket runtime-config](configuration.md#pocket-runtime-config)」を参照してください。

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
| `--yes`, `-y` | 確認プロンプトをスキップ |
| `--skip-check-existing` | neon/tidb/upstash の存在確認 API をスキップ |

!!! note "`pocket deploy` との違い"
    `pocket deploy` はインフラのデプロイのみ行います。
    `pocket django deploy` はインフラデプロイに加え、ローカルでの `collectstatic` + S3アップロード、Lambda上での `migrate` も対話形式で実行します。

### pocket django build

現在の作業ツリーからコンテナイメージをビルドし、git commit hash（full）をタグにして
ECR へ push します。デプロイは行いません。

```bash
pocket django build --stage=dev
```

- タグは `COMMIT_HASH` 環境変数があればそれを、なければ `git rev-parse HEAD` を使います
  （CI では `COMMIT_HASH=${{ github.sha }}` のように渡せます）
- commit hash とイメージ内容の一致が前提のため、作業ツリーに未コミットの変更がある
  場合はエラーになります（`--allow-dirty` で回避できますが、そのイメージの昇格は
  推奨しません）

| オプション | 説明 |
|-----------|------|
| `--stage` | 対象ステージ |
| `--allow-dirty` | 作業ツリーが dirty でもビルドする（ローカル検証用） |

### pocket django promote

[`pocket promote`](#pocket-promote) に加え、ローカルでの `collectstatic` + S3アップロード、
Lambda 上での `migrate` も対話形式で実行します（`pocket django deploy` の昇格版）。

```bash
pocket django promote --stage=stg --commit-hash=<full-sha>
```

| オプション | 説明 |
|-----------|------|
| `--stage` | 対象ステージ |
| `--commit-hash` | 昇格するイメージの git commit hash（**必須**） |
| `--openpath` | デプロイ後にブラウザで開くパス |
| `--yes`, `-y` | 確認プロンプトをスキップ |
| `--skip-check-existing` | neon/tidb/upstash の存在確認 API をスキップ |

### build once と昇格 {#build-once}

`pocket django deploy` は実行のたびにイメージをビルドしますが、`build` + `promote` を
使うと **一度ビルドした同一イメージを複数ステージへ再ビルドなしで昇格**できます
（build once）。

```bash
# 1. 一度だけビルド（:<full-sha> タグで push）
pocket django build --stage=dev

# 2. 同じイメージを各ステージへ昇格（再ビルドなし）
pocket django promote --stage=dev --commit-hash=<full-sha>
pocket django promote --stage=stg --commit-hash=<full-sha>
```

- **イメージはステージ非依存です。** `pocket.runtime.toml` は全ステージの設定を含んで
  イメージに焼き込まれ、ステージは Lambda の `POCKET_STAGE` 環境変数で実行時に解決
  されます。`build --stage=dev` の `--stage` は ECR リポジトリ等の対象を決めるだけで、
  生成されるイメージ自体はどのステージでも動きます。
- **ステージ間で ECR リポジトリを共有するには** [`[awscontainer].ecr_name`](configuration.md#awscontainer)
  を設定します。デフォルトでは ECR リポジトリ名にステージ名が含まれるため、
  ステージごとに別リポジトリになり昇格が成立しません。同一 AWS アカウント内の
  ステージで同じ `ecr_name` を指定すると、昇格がタグの付け替えだけで完結します。
- **静的アセット（SPA ビルド + collectstatic）は昇格時も再ビルドされます。**
  build once の対象はコンテナイメージのみです。
- 通常の `pocket django deploy` の挙動は変わりません（commit hash タグも付きません）。
  開発ループでは従来どおり `deploy` を、リリースフローでは `build` + `promote` を
  使い分けてください。

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
| `--timeout-seconds` | ログ表示のタイムアウト（秒） |

### pocket django resetdb

データベースの public スキーマをリセットします。
Lambda 経由で `DROP SCHEMA public CASCADE; CREATE SCHEMA public;` を実行し、全テーブルを削除します。

```bash
pocket django resetdb --stage=dev
```

| オプション | 説明 |
|-----------|------|
| `--stage` | 対象ステージ |
| `--yes`, `-y` | 確認プロンプトをスキップ |

リセット後は `pocket django manage migrate --stage=dev` でマイグレーションをやり直してください。

!!! warning "破壊的操作"
    全テーブルとデータが削除されます。本番環境での実行には十分注意してください。

### pocket django deploystatic

静的ファイルのみをデプロイします（ローカルで `collectstatic` → S3にアップロード）。

```bash
pocket django deploystatic --stage=dev
```

| オプション | 説明 |
|-----------|------|
| `--stage` | 対象ステージ |
| `--skip-collectstatic` | collectstaticをスキップしてアップロードのみ実行 |
| `--delete` | collectstatic 出力に無い S3 上のファイルを削除する (`aws s3 sync --delete`) |
| `--link` / `--no-link` | collectstatic に `--link` を渡す (大容量資産の複製コスト削減)。省略時は staticfiles 宣言の `link` に従う |

!!! note "`--delete` は opt-in"
    デフォルトではアップロードのみで、S3 上の既存ファイルは削除しません。
    削除すると、旧デプロイのアセットを参照中のリクエスト (キャッシュ済み HTML や
    切替前の Lambda が返すページ) や過去 commit への rollback が壊れる可能性が
    あるためです。不要ファイルの掃除をしたいときだけ `--delete` を付けてください。

静的ファイルの publish を deploy / promote から切り離したい場合
(CI と資産更新で publish 経路を分ける等) は、staticfiles 宣言に
`publish = "command"` を指定します ([Django ストレージ設定](configuration.md) 参照)。

link を常用する場合は、フラグではなく staticfiles 宣言に `link = true` を
指定してください。deploy / promote 内の collectstatic にも適用され、
link 有効時はビルド先を collectstatic 前に毎回クリアします
(非 link 実体との混在と壊れ symlink の残留を避けるため。同ドキュメント参照)。

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

# CFNスタックの作成 / 更新（通常は pocket deploy 経由で実行）
pocket resource awscontainer create --stage=dev
pocket resource awscontainer update --stage=dev

# SSM/Secrets Manager の最新値で Lambda 環境変数を即時更新（CFNを介さない）
pocket resource awscontainer reload-env --stage=dev

# Lambda の現在の環境変数と SSM/Secrets Manager 上の宣言値の差分を表示
pocket resource awscontainer status-env --stage=dev

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

### image

pocket がビルド・デプロイしたコンテナイメージの参照情報を出力します。外部ツール
（pocket と併走するデプロイ系）が pocket の内部命名を再実装せずにイメージを参照
するための導線です。stdout には値のみが出るため `$(...)` でスクリプトから使えます。

```bash
# ECR リポジトリ名（ecr_name 上書きを含め pocket.toml 準拠）
pocket resource image repo --stage=sandbox

# deploy 済み実イメージの URI（{repo_uri}@{digest}。以後の deploy でタグが動いても壊れない）
pocket resource image uri --stage=sandbox
```

toml を読まない静的導出には Python API の `pocket.naming.ecr_repo_name()` /
`pocket.naming.ecr_image_tag()` も利用できます（`ecr_name` や `prefix_template` を
上書きしている構成では引数で渡す必要があります。詳細は docstring 参照）。

### neon

```bash
# Neon ブランチの状態確認
pocket resource neon status --stage=dev

# コンテキスト（テンプレートに渡される変数）を表示
pocket resource neon context --stage=dev

# ブランチの作成（通常は pocket deploy 経由で実行）
pocket resource neon create --stage=dev

# データベースの削除 + 再作成
pocket resource neon reset-database --stage=dev

# 別ステージのブランチから分岐して作成
pocket resource neon branch-out --stage=feature1 --base-stage=dev

# ブランチの削除
# (root branch は Neon 仕様で単体削除できないため、project 内に他の branch が
#  なければ再確認のうえ project ごと削除。他の branch が残る場合はエラーで中断)
pocket resource neon delete --stage=dev

# provisioning="command" 用: branch/role/db を ensure し DATABASE_URL を stored user
# secret に保存（既存があれば --force で上書き）
pocket resource neon store-url --stage=dev
```

`store-url` は保存前に、解決した接続先を表示します（pocket.toml の意図と目視照合
できます）。この実行で branch を新規作成した場合（= 空の branch に接続する）は
警告が添えられるため、既存データへ接続するつもりで意図しない branch 名を焼いた
ことに気づけます:

```
stage=dev → project=dev-myapp → branch=main → endpoint=ep-misty-cherry-xxxx.ap-southeast-1.aws.neon.tech
Neon URL を DATABASE_URL (/dev-myapp-pocket-user/neon_database_url) に保存しました。
```

### tidb

```bash
# TiDB クラスターの状態確認
pocket resource tidb status --stage=dev

# コンテキストを表示
pocket resource tidb context --stage=dev

# クラスターの作成（通常は pocket deploy 経由で実行）
pocket resource tidb create --stage=dev

# データベースの削除 + 再作成
pocket resource tidb reset-database --stage=dev

# クラスターの削除
pocket resource tidb delete --stage=dev

# provisioning="command" 用: cluster/db を ensure し DATABASE_URL を stored user secret に
# 保存（既存があれば --force で上書き。TiDB は実行ごとに root password をローテーション）
pocket resource tidb store-url --stage=dev
```

### upstash

```bash
# provisioning="command" 用: database を ensure し REDIS_URL を stored user secret に保存
# （既存があれば --force で上書き）
pocket resource upstash store-url --stage=dev
```

### dsql

```bash
# DSQL クラスターの状態確認
pocket resource dsql status --stage=dev

# 接続情報の表示（endpoint, region, port）
pocket resource dsql endpoint --stage=dev

# 機械可読な JSON で stdout に出力（スクリプト / CI 向け。クラスター不在時は exit 1）
pocket resource dsql endpoint --stage=dev --format=json
# => {"endpoint": "xxxxx.dsql.ap-northeast-1.on.aws", "region": "ap-northeast-1", "port": 5432}

# オンデマンドバックアップの開始（AWS Backup。DSQL に組み込みの自動バックアップは
# 無いため、これが唯一のバックアップ手段。--watch で完了まで待機）
# 前提リソース（vault "pocket-backup" とサービスロール "forge-pocket-backup-role"）は
# 初回実行時に冪等に自動作成される
pocket resource dsql backup --stage=dev
pocket resource dsql backup --stage=dev --watch

# 既存の vault・サービスロールを使う場合や保持日数の指定（指定したものは存在確認しない）
pocket resource dsql backup --stage=dev --vault=my-vault \
  --iam-role-arn=arn:aws:iam::123456789012:role/my-backup-role --retention-days=35

# 明示的に無期限で残す（pocket からは削除できなくなるため警告が出る）
pocket resource dsql backup --stage=dev --retention-days=0

# バックアップ job の状態確認（--job-id 省略時はこのクラスターの最新 job。
# --watch で終端状態まで待機。aws CLI 直接でも可:
#   aws backup describe-backup-job --backup-job-id <id> --region <region>）
pocket resource dsql backup-status --stage=dev
pocket resource dsql backup-status --stage=dev --job-id=<id> --watch

# バックアップからの復元（新しいクラスターを作成して現用に切り替える）
# 復元前に現用クラスターのバックアップを取るか確認する（--skip-backup で省略、
# --yes で自動承認）。restore job の完了まで待機する
pocket resource dsql restore --stage=dev --latest
pocket resource dsql restore --stage=dev <recovery-point-arn>

# 待機が中断された場合に、切り替えを再開・完了させる（冪等）
pocket resource dsql restore-status --stage=dev --job-id=<id>

# クラスターの削除（確認プロンプト付き）
pocket resource dsql destroy --stage=dev
```

!!! info "オンデマンドバックアップの保持日数"
    `--retention-days` を省略した場合の保持日数は **`[backup.dsql]` の最長階層（monthly）の `delete_after_days`（既定 1095 日 = 3 年）→ 宣言が無ければ 1095 日** の順で決まります。解決結果は実行時に `Retention: 1095 days (from [backup.dsql] monthly)` のように表示されます。復元前に取られる現用クラスターのバックアップも同じ解決を通ります。

    保持日数を渡さないと AWS Backup の recovery point は**無期限**に残ります。pocket がバックアップ**データ**を削除するのは `[backup]` の `deletable = true` 宣言 + 明示確認の経路だけのため（[設定ファイル](configuration.md)の「backup」節を参照）、無期限の recovery point は放置すると残り続けます。既定で失効日を付けているのはこのためです。

    意図して無期限にする場合は `--retention-days=0` を明示してください（警告を出したうえで `Lifecycle` を付けずに実行します）。

!!! info "定期バックアップの job が一覧に出るまでのラグ"
    `[backup.dsql]` の plan 由来の job は、スケジュール時刻を過ぎても `backup-status`（AWS Backup の `ListBackupJobs`）やコンソールの一覧にすぐには現れません。実測で **30 分ほど遅れて** `CREATED` として見え始めた例があります（`CreationDate` はスケジュール時刻のまま）。

    さらに AWS Backup は指定時刻ちょうどではなく start window 内で開始するため、**一覧に出ない = 実行されなかった、ではありません**。定期実行の確認は時間に余裕を持って行ってください。

!!! warning "復元後は deploy が必要です"
    復元は常に**新しいクラスター**を作成します（AWS Backup は既存クラスターを上書きしません）。`restore` は Name タグの付け替えと SSM の endpoint 更新まで行いますが、**Lambda の `POCKET_DSQL_ENDPOINT` と `dsql:DbConnectAdmin` の対象 ARN は CloudFormation 管理**のため、`pocket deploy` を実行するまでアプリは旧クラスターに書き込み続けます。

    旧クラスターは削除しません（復元結果が期待どおりでなかったときの戻り先）。Name タグを `<tag_name>-replaced-<identifier>` へ退避するだけなので、**明示的に削除するまで課金され続けます**。

    マルチリージョンクラスターの復元には witness / peer リージョンの指定が必要で、pocket は対応していません（AWS Backup のコンソールまたは CLI を使用してください）。

### rds

```bash
# RDS Aurora クラスターの状態確認
pocket resource rds status --stage=dev

# 接続情報の表示（endpoint, port, database, username）
pocket resource rds endpoint --stage=dev

# 機械可読な JSON で stdout に出力（スクリプト / CI 向け。クラスター不在時は exit 1）
pocket resource rds endpoint --stage=dev --format=json

# クラスターの削除（確認プロンプト付き、Final Snapshot 作成）
pocket resource rds destroy --stage=dev
```

### s3

```bash
# S3バケットの状態確認
pocket resource s3 status --stage=dev

# コンテキストを表示
pocket resource s3 context --stage=dev

# バケットの作成（通常は pocket deploy 経由で実行）
pocket resource s3 create --stage=dev

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

# CFNスタックの作成 / 更新 / 削除（通常は pocket deploy / destroy 経由で実行）
pocket resource cloudfront create --stage=dev
pocket resource cloudfront update --stage=dev
pocket resource cloudfront destroy --stage=dev

# フロントエンドのビルド・S3アップロード・キャッシュ無効化
pocket resource cloudfront upload --stage=dev

# ビルドをスキップしてアップロードのみ
pocket resource cloudfront upload --stage=dev --skip-build

# 特定のディストリビューションのみ
pocket resource cloudfront upload --stage=dev --name=main
```

`upload` は `build` / `upload_dir` が設定されたルートに対して、ビルド（`build` のルートのみ）→ S3アップロード → CloudFrontキャッシュ無効化を実行します。
`pocket deploy` 実行時にも自動的に呼ばれます（`--skip-frontend` で抑制可能）。

アップロードは**差分のみ**です。S3 上のオブジェクトと内容が一致するファイルはスキップされるため、数千ファイル規模のアセットを `upload_dir` に置いても deploy 時間は伸びません（判定は ETag。詳細は [routes](configuration.md#routes) を参照）。
キャッシュ無効化も**変更があったルートの `path_pattern` に限定**され、変更が無ければ invalidation 自体を発行しません。

### cloudfront_keys

CloudFront 署名付き URL 用の鍵リソースを管理します。`signing_key` が設定されたディストリビューションのみ対象です。

```bash
# CloudFormation YAMLを表示
pocket resource cloudfront-keys yaml --stage=dev

# CloudFormation YAMLの差分を確認
pocket resource cloudfront-keys yaml-diff --stage=dev

# 状態確認
pocket resource cloudfront-keys status --stage=dev

# 鍵リソースの削除
pocket resource cloudfront-keys destroy --stage=dev

# 特定のディストリビューションのみ
pocket resource cloudfront-keys yaml --stage=dev --name=media
```

### cloudfront_waf

CloudFront にアタッチする WAFv2 (IP allowlist) リソースを管理します。`waf` が設定されたディストリビューションのみ対象です。

```bash
# 状態確認
pocket resource cloudfront-waf status --stage=dev

# CloudFormation YAMLを表示
pocket resource cloudfront-waf yaml --stage=dev

# CloudFormation YAMLの差分を確認
pocket resource cloudfront-waf yaml-diff --stage=dev

# WAFリソースの削除
pocket resource cloudfront-waf destroy --stage=dev
```

### vpc

```bash
# VPCの状態確認
pocket resource vpc status

# CloudFormation YAMLを表示
pocket resource vpc yaml

# CloudFormation YAMLの差分を確認
pocket resource vpc yaml-diff

# CFNスタックの作成 / 更新（通常は pocket deploy 経由で実行）
pocket resource vpc create
pocket resource vpc update

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
