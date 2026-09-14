# example-dsql

Aurora DSQL + Rust (axum) の example。`example-neon` / `example-tidb` が Django なのに対し、
こちらは **pocket-rs (Rust runtime) と DSQL の組み合わせ**を実機で示す。

## 構成

```
EventBridge Scheduler ──(1 日 1 回)──▶ SQS queue ──▶ Lambda worker ──▶ Aurora DSQL
                                          │                              │
                                          └──▶ DLQ                       │
SES 受信 ──▶ S3 (原本) ──▶ SNS ──▶ SQS queue ──▶ Lambda mail worker ─────┤
                                     │                                   │
                                     └──▶ 配送 DLQ                       ▼
                          CloudFront ──/api/*──▶ Lambda (axum) ────── SELECT
                                     └──既定──▶ S3 (SvelteKit SPA)
```

| 要素 | 実装 |
|---|---|
| HTTP | axum + `lambda_http` (`pocket-example-dsql-lambda`) |
| worker | `lambda_runtime` + `magic_pocket_rs::sqs::process_sqs_records` (`pocket-example-dsql-worker`) |
| mail worker | `lambda_runtime` + `magic_pocket_rs::inbound::process_inbound_records` (`pocket-example-dsql-mail-worker`) |
| DB | Aurora DSQL (IAM 認証、トークンはアプリ内生成) + SeaORM |
| フロント | SvelteKit (adapter-static) の SPA |
| 定期実行 | `pocket.sqs_scheduler` で 1 日 1 回 |
| メール受信 | `[inbound.inbox]` (SES → S3 → SNS → SQS)。要約を `mails` に登録し `/api/mails` で一覧 |

## なぜ prune もページングも無いのか

1 日 1 行しか増えないため、数千行に達するまで年単位かかる。デモとして単純さを
優先し、一覧 API はページングなしの素の `SELECT` にしてある。無制限に伸びる
設計ではあるので、実運用ではページングか期間絞り込みを入れる。

!!! note "DSQL の 3,000 行制限との関係"
    よく混同されるが、DSQL の 3,000 行制限は **1 トランザクションで変更できる
    行数** (DML: `INSERT` / `UPDATE` / `DELETE`) の上限であって、**`SELECT` の
    取得行数の制限ではない**。この example は 1 トランザクション 1 行の `INSERT`
    だけなので抵触しない。ただし将来まとめて削除する場合は、3,000 行以下に
    チャンク分割する必要がある。

## なぜ 1 日 1 回なのか

サーバーレス DB の無料枠は、短周期の定期実行で静かに焼き切れる。`example-tidb` で
15 分毎のハートビートを常設していたところ、無料枠の枯渇後も定期実行が止まらず、
失敗メッセージが再試行されて DLQ に 1,000 通以上滞留した。**経路が生きていることを
示すだけなら 1 日 1 回で足りる。**

## ローカル開発

```sh
just app          # axum (http://0.0.0.0:8000)
just front        # SvelteKit dev server (port 3000、/api は 8000 へ proxy)
just test         # cargo test
just lint         # clippy -D warnings
```

DB 接続系の環境変数が 1 つも無ければ **DB 無しで起動する** (`/api/health` の
`db_configured` が `false` になる)。ローカル PostgreSQL を使う場合は `PG_HOST` 系、
DSQL を直接使う場合は `DSQL_HOST` を与える (`src/config.rs`)。

## スキーマ管理

信頼の源は `schema.sql`。DSQL 方言の SQL を `migrations/` に置き、`just schema-apply`
で 1 文ずつ適用する (適用済みは `schema_migrations` に記録され、再実行で飛ばされる)。

```sh
DSQL_HOST=<endpoint> just schema-apply
```

DSQL の制約が素の PostgreSQL と違う点:

- `FOREIGN KEY` は使えない (参照整合性はアプリ層で担保)
- `UPSERT` (`INSERT ... ON CONFLICT`) は使えない (delete + insert で代替)
- `SERIAL` は使えない (主キーは `uuid` + `gen_random_uuid()`)
- 二次インデックスは `CREATE INDEX ASYNC` (作成は非同期。進行は `sys.jobs`)
- DDL と DML は別トランザクション。1 トランザクションに DDL は 1 文まで
- 1 トランザクションで変更できるのは 3,000 行まで (DML)

一次情報は AWS の
[Migrating from PostgreSQL to Aurora DSQL](https://docs.aws.amazon.com/aurora-dsql/latest/userguide/working-with-postgresql-compatibility-unsupported-features.html)
と [Cluster quotas and database limits](https://docs.aws.amazon.com/aurora-dsql/latest/userguide/CHAP_quotas.html)。
「PostgreSQL 互換だから」と通常の PostgreSQL の常識で書くと確実にハマる。

運用プロジェクトでは専用のマイグレーションツールを使うが、この example は
テーブルが 1 つだけなので依存を増やさず SeaORM の生 SQL 実行で完結させている
(`src/bin/schema_apply.rs`)。

## メール受信 (inbound)

`pocket.toml` の `[inbound.inbox]` が SES の受信口。pocket が原本を非公開 S3 に保存し、
SNS → SQS で `main.mail` handler (`src/bin/mail_worker.rs`) に届く。worker は
`magic_pocket_rs::inbound` が原本の版・hash と受信情報を保全した後で、件名 / From /
実際の宛先を `mails` テーブルに登録する (受信 ID で冪等)。本文・添付は読まない。

初回だけ受信ドメインの初期設定が要る (`domain` も placeholder なので実値へ書き戻す):

```sh
/app/.venv/bin/pocket resource inbound --stage sandbox --name inbox init   # TXT / MX を出力
# 出力された TXT (_amazonses.<domain>) と MX を DNS に登録し、SES の検証完了を待つ
/app/.venv/bin/pocket deploy --stage sandbox -y
DSQL_HOST=<endpoint> just schema-apply                                    # mails テーブル
```

deploy 後、`delivery_alert` / `dead_letter_alert` の SNS 購読確認メールのリンクを開く。
検証は受信アドレス宛にメールを送り、`/api/mails` (画面の「受信メール」) に要約が出ること、
inbound bucket の `raw/` と `metadata/` に原本と受信情報が揃うことを確認する。

## デプロイ (sandbox)

`pocket.toml` の `domain` は公開 repo では placeholder。**deploy 前に実ドメインへ
書き戻し、終わったら placeholder に戻す** (実値は機密メモが SoT)。

```sh
just front-build                                  # pocket は frontend/build を再ビルドしない
/app/.venv/bin/pocket deploy --stage sandbox -y   # CFn + SPA (DB に触らない)
DSQL_HOST=<endpoint> just schema-apply            # 初回のみ: テーブルを作る
```

デプロイ前チェック:

- ファイルの other-read (`find . -type f -not -perm -o=r`)。mode 600 のファイルが
  image に入ると Lambda の非 root 実行ユーザーが読めず INIT で落ちる
- AWS 認証が worktree に合っているか (`aws sts get-caller-identity`)
