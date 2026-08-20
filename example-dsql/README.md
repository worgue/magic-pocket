# example-dsql

Aurora DSQL + Rust (axum) の example。`example-neon` / `example-tidb` が Django なのに対し、
こちらは **pocket-rs (Rust runtime) と DSQL の組み合わせ**を実機で示す。

## 構成

```
EventBridge Scheduler ──(1 日 1 回)──▶ SQS queue ──▶ Lambda worker ──▶ Aurora DSQL
                                          │                              │
                                          └──▶ DLQ                       │
                                                                         ▼
                          CloudFront ──/api/*──▶ Lambda (axum) ────── SELECT
                                     └──既定──▶ S3 (SvelteKit SPA)
```

| 要素 | 実装 |
|---|---|
| HTTP | axum + `lambda_http` (`pocket-example-dsql-lambda`) |
| worker | `lambda_runtime` + `magic_pocket_rs::sqs::process_sqs_records` (`pocket-example-dsql-worker`) |
| DB | Aurora DSQL (IAM 認証、トークンはアプリ内生成) + SeaORM |
| フロント | SvelteKit (adapter-static) の SPA |
| 定期実行 | `pocket.sqs_scheduler` で 1 日 1 回 |

## なぜ prune もページングも無いのか

DSQL は **1 クエリで取得できる行数が 3,000 件**に制限される。このデモは 1 日 1 行しか
追加せず削除もしないので、**約 8.2 年 (3,000 日) は素の `SELECT` で全件返せる**。
上限に当たったら一覧 API にページングか期間絞り込みが必要になる。
「上限に当たるまでは単純に書く」という判断を意図的に残している
(根拠は `schema.sql` の `messages` テーブルのコメント)。

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
- `SERIAL` は使えない (主キーは `uuid` + `gen_random_uuid()`)
- 二次インデックスは `CREATE INDEX ASYNC` (作成は非同期。進行は `sys.jobs`)
- 1 トランザクションに DDL は 1 文まで

運用プロジェクトでは専用のマイグレーションツールを使うが、この example は
テーブルが 1 つだけなので依存を増やさず SeaORM の生 SQL 実行で完結させている
(`src/bin/schema_apply.rs`)。

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
