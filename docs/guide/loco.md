# Rust 連携（Loco / 素の axum）

Rust アプリケーションでは、`magic-pocket-rs` crate を使って環境変数をセットアップします。
Django の `set_envs()` に相当する機能を Rust で提供します。

ここで使う仕組み（`lambda_http` で axum Router を Lambda ハンドラーにする・`set_envs()`
でシークレット/リソース情報を注入する・`pocket.toml` の `handlers.command` でバイナリを
指定する）は **Loco 固有ではなく、`lambda_http` を使う任意の axum アプリで共通**です。
Loco を使わない素の axum バイナリでも、同じ `pocket.toml` と Dockerfile でそのまま
デプロイできます（→[Lambda エントリポイント](#lambda)に両方の例）。デプロイは
フレームワークを問わず plain `pocket deploy` で完結します（Loco 専用のデプロイ
コマンドはありません）。

---

## セットアップ

`Cargo.toml` に依存を追加します。

```toml
[dependencies]
magic-pocket-rs = { git = "https://github.com/worgue/magic-pocket.git" }
lambda_http = "0.14"
tokio = { version = "1", features = ["full"] }
# Loco を使う場合のみ:
loco-rs = "0.14"
# 素の axum の場合は axum を直接:
# axum = "0.8"
```

Django では `apig-wsgi` が WSGI アプリを Lambda ハンドラーに変換しますが、axum では [`lambda_http`](https://crates.io/crates/lambda_http) が axum Router を直接 Lambda ハンドラーとして使えます（Loco の Router も素の axum の Router も同じ）。フレームワーク側で完結するため、Lambda Web Adapter のような外部 Extension は不要です。

---

## Lambda エントリポイント

`src/bin/lambda.rs` を作成します。共通するのは「`set_envs()` で環境変数を注入 →
axum Router を組み立てて `lambda_http::run(router)` に渡す」という流れで、Router の
作り方だけがフレームワークで変わります。

### Loco の場合

```rust
use loco_rs::boot::{create_app, StartMode};
use loco_rs::config::Config;
use loco_rs::environment::Environment;

use myapp::app::App;
use myapp::migration::Migrator;

#[tokio::main]
async fn main() -> Result<(), lambda_http::Error> {
    magic_pocket_rs::set_envs().await.unwrap();

    let environment: Environment = "production".parse().unwrap();
    let config = Config::new(&environment)?;
    let boot = create_app::<App, Migrator>(
        StartMode::ServerOnly,
        &environment,
        config,
    )
    .await?;

    lambda_http::run(boot.router.expect("no router")).await
}
```

### 素の axum の場合

Loco を使わず axum の Router を直接組む場合も、`set_envs()` → `lambda_http::run()` の
流れは同じです。

```rust
use axum::{routing::get, Router};

#[tokio::main]
async fn main() -> Result<(), lambda_http::Error> {
    magic_pocket_rs::set_envs().await.unwrap();

    let router = Router::new().route("/api/health", get(|| async { "ok" }));

    lambda_http::run(router).await
}
```

この場合 `Cargo.toml` の `loco-rs` は不要で、`axum` を直接依存に加えます。以降の
`set_envs()` / `pocket.toml` / Dockerfile はフレームワークを問わず共通です
（後述の「注意点」の `production.yaml` / static 設定は Loco 固有なので素の axum では
不要）。

---

## set_envs()

`set_envs()` は、Secrets Manager / SSM からシークレットを取得し、CloudFormation Output から API Gateway のホスト情報を取得して、すべて環境変数にセットします。セットされる環境変数の一覧は「[実行環境](runtime.md#セットされる環境変数)」を参照してください。

| 関数 | 説明 |
|------|------|
| `set_envs()` | シークレット + AWS リソース情報をすべてセット |
| `set_envs_from_secrets(stage)` | シークレットのみセット |
| `set_envs_from_resources(stage)` | AWS リソース情報のみセット |

`POCKET_STAGE` が設定されていない場合、シークレット取得はスキップされます（ローカル環境での安全動作）。

---

## pocket.toml の構成例

```toml
[general]
region = "ap-northeast-1"
stages = ["dev", "prod"]

[s3]

[neon]
project_name = "dev-myproject"

[awscontainer]
dockerfile_path = "pocket.Dockerfile"

[awscontainer.handlers.wsgi]
command = "myapp-lambda"

[dev.awscontainer.handlers.wsgi]
apigateway = {}

[awscontainer.secrets.managed]
LOCO_SECRET_KEY = { type = "password", options = { length = 50 } }
DATABASE_URL = { type = "neon_database_url" }
```

### データベースの選択

`set_envs()` は Django 版と同じ DB backend に対応しています。`DATABASE_URL` の
`type` を差し替えるだけで切り替わります。

| type | 備考 |
|------|------|
| `neon_database_url` | 上記の例。VPC 不要 |
| `tidb_database_url` | VPC 不要 |
| `rds_database_url` | VPC 必須。認証情報から実行時に `DATABASE_URL` を構築 |
| （DSQL） | `[dsql]` を設定。IAM 認証トークンを `POCKET_DSQL_TOKEN` にセット |

RDS を使う場合は `[rds]` と VPC の設定が必要です。

```toml
[vpc]
ref = "main"
zone_suffixes = ["a", "c"]  # managed VPC では RDS に 2AZ 以上必須

[rds]

[awscontainer.secrets.managed]
DATABASE_URL = { type = "rds_database_url" }
```

RDS のマスターパスワードは自動ローテーションされるため、`DATABASE_URL` は
deploy 時ではなく `set_envs()` 呼び出し時に認証情報から構築されます。詳細は
「[実行環境](runtime.md#セットされる環境変数)」を参照してください。

---

## Dockerfile の例

```dockerfile
FROM rust:1-bookworm AS builder
WORKDIR /app
COPY . .
RUN cargo build --release --bin myapp-lambda

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app/target/release/myapp-lambda /usr/local/bin/
COPY --from=builder /app/config /app/config
COPY pocket.runtime.toml /app/pocket.runtime.toml
WORKDIR /app
CMD ["myapp-lambda"]
```

`command` は Docker の CMD をオーバーライドするため、Dockerfile では `CMD` のみを使用し `ENTRYPOINT` は設定しないことを推奨します。詳細は「[設定ファイル - awscontainer.handlers](configuration.md#awscontainerhandlers)」を参照してください。

---

## 注意点

以下は Loco の設定ファイル（`production.yaml`）に関する注意で、**Loco 固有**です。
素の axum アプリでは該当しません。

### production.yaml の環境変数クォート

magic-pocket が生成するシークレット（`type = "password"`）には `*`, `!`, `#` 等の YAML 特殊文字が含まれる場合があります。Loco の設定テンプレートでは環境変数を**必ずクォートで囲んで**ください。

```yaml
# NG - SECRET_KEY に * が含まれると YAML パースが壊れる
secret: {{ get_env(name="SECRET_KEY") }}

# OK
secret: "{{ get_env(name="SECRET_KEY") }}"
database:
  uri: "{{ get_env(name="DATABASE_URL") }}"
```

### static ミドルウェアの無効化

Loco のデフォルト `production.yaml` には `static.must_exist: true` で `frontend/dist` を参照する設定があります。Lambda コンテナにフロントエンドを含めない場合は無効にしてください。

```yaml
static:
  enable: false
```

### DB 接続タイムアウト

Neon がアプリケーションと異なるリージョンにある場合、デフォルトの接続タイムアウトでは不足する場合があります。クロスリージョン接続では余裕を持った値を設定してください。

```yaml
database:
  connect_timeout: 5000
  idle_timeout: 5000
```

!!! tip "Neon のリージョン選択"
    Neon プロジェクトは Lambda と同じリージョン（または近いリージョン）に作成することを推奨します。レイテンシと接続タイムアウトの問題を回避できます。
