# Loco連携

Rust アプリケーション（Loco など）では、`magic-pocket-rs` crate を使って環境変数をセットアップします。
Django の `set_envs()` に相当する機能を Rust で提供します。

---

## セットアップ

`Cargo.toml` に依存を追加します。

```toml
[dependencies]
magic-pocket-rs = { git = "https://github.com/worgue/magic-pocket.git" }
lambda_http = "0.14"
loco-rs = "0.14"
tokio = { version = "1", features = ["full"] }
```

Django では `apig-wsgi` が WSGI アプリを Lambda ハンドラーに変換しますが、Loco（axum）では [`lambda_http`](https://crates.io/crates/lambda_http) が axum Router を直接 Lambda ハンドラーとして使えます。フレームワーク側で完結するため、Lambda Web Adapter のような外部 Extension は不要です。

---

## Lambda エントリポイント

`src/bin/lambda.rs` を作成します。

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
stages = ["dev", "prd"]

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
COPY --from=builder /app/pocket.toml /app/pocket.toml
WORKDIR /app
CMD ["myapp-lambda"]
```

`command` は Docker の CMD をオーバーライドするため、Dockerfile では `CMD` のみを使用し `ENTRYPOINT` は設定しないことを推奨します。詳細は「[設定ファイル - awscontainer.handlers](configuration.md#awscontainerhandlers)」を参照してください。

---

## 注意点

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
