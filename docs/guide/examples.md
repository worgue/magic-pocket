# サンプルプロジェクト

リポジトリには動作する example が 3 つ入っています。どれも実際に AWS へデプロイして
動かしているもので、pocket.toml の書き方を読むならここが一番確実です。

| example | ランタイム | DB | 主に示すもの |
|---------|-----------|-----|-------------|
| [`example-neon`](https://github.com/worgue/magic-pocket/tree/main/example-neon) | Django | Neon (PostgreSQL) | 最小構成。Django + CloudFront + S3 静的配信 |
| [`example-tidb`](https://github.com/worgue/magic-pocket/tree/main/example-tidb) | Django | TiDB Serverless (MySQL) | `type =` による stored user secret、MySQL 系バックエンド |
| [`example-dsql`](https://github.com/worgue/magic-pocket/tree/main/example-dsql) | Rust (axum) | Aurora DSQL | pocket-rs、SQS worker、`pocket.sqs_scheduler` による定期実行、SPA (SvelteKit) |

## それぞれの読みどころ

### example-neon — まずこれを読む

Django アプリを Lambda に載せる最小の形です。

- `[container.main.handlers.wsgi]` / `[handlers.management]` の 2 handler 構成
- `[container.main.django.storages]` で staticfiles を S3 + CloudFront に流す
- `[container.main.secrets.managed]` の `SECRET_KEY` 自動生成
- DB 接続 URL は `provisioning = "command"` で事前に SSM へ保存する形
  (deploy は Neon の API を叩きません)

`[general] region` が `ap-southeast-1` なのは意図的です。Neon は東京リージョンに
対応しておらず、Lambda を東京に置くと DB アクセスがクロスリージョンになるためです。

### example-tidb — stored user secret の型指定

`DATABASE_URL = { type = "tidb_database_url" }` のように **型で宣言**し、
パラメータ名を書かない形を示しています。外部の provisioner が正準パス
(`/{stage}-{project}-{namespace}-user/{type}`) へ値を put すれば、consumer 側は
`name =` で名前を手書きしなくても読めます。

### example-dsql — Rust + 非同期 worker + SPA

Python 以外のランタイムと、queue を挟んだ定期実行の例です。

```
EventBridge Scheduler ──(1 日 1 回)──▶ SQS ──▶ Lambda worker ──▶ Aurora DSQL
                                        │                          │
                                        └──▶ DLQ                   │
                                                                   ▼
                        CloudFront ──/api/*──▶ Lambda (axum) ─── SELECT
                                   └──既定──▶ S3 (SvelteKit SPA)
```

- 1 つのイメージに HTTP と worker の両バイナリを焼き、handler ごとに CMD で切替
- worker は `magic_pocket_rs::sqs::process_sqs_records` で partial batch response を
  返すため、1 record の失敗が同じバッチの成功分を巻き戻しません
- `[dsql]` を書くだけでクラスターが作られ、IAM 認証トークンはアプリ内で生成します

!!! tip "定期実行の頻度は控えめに"
    この example の schedule は **1 日 1 回**です。sandbox の常設デモを短い周期で
    回すと、サーバーレス DB の無料枠を静かに使い切ります。経路が生きていることを
    示すだけなら 1 日 1 回で足ります。

## 動かすときの注意

- **`domain` は placeholder です。** 公開リポジトリに実ドメインを置けないため、
  `[<stage>.cloudfront.web] domain` は `*.example.com` になっています。
  自分の環境で動かすときは実ドメイン (Route53 の hosted zone 配下) に書き換えてください
- **SPA を持つ example は先にフロントをビルドします。** `pocket deploy` は
  `frontend/build` を再ビルドしません (`just front-build` を先に実行してください)
- **ビルドコンテキストのファイルは other-read が必要です。** mode 600 のファイルが
  イメージに入ると、Lambda の非 root 実行ユーザーが読めず INIT で失敗します
  (0.31.0 以降、`pocket.toml` / `pocket.runtime.toml` については deploy 時にエラーで止まります)
