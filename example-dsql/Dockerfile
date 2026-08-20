FROM rust:1.94-bookworm AS builder

# cmake は aws-lc-sys のビルドに必須 (rust イメージは cmake を含まない)
RUN apt-get update && apt-get install -y --no-install-recommends cmake \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
# 1 イメージに HTTP と worker の両方を焼き、handler ごとに CMD で切り替える
# (pocket.toml の [container.main.handlers.<key>] command が CMD を上書きする)
RUN cargo build --release --bin pocket-example-dsql-lambda --bin pocket-example-dsql-worker

FROM public.ecr.aws/docker/library/debian:bookworm-slim

# ca-certificates は DSQL 等への TLS 接続に必要
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /var/task
COPY --from=builder /app/target/release/pocket-example-dsql-lambda /usr/local/bin/pocket-example-dsql-lambda
COPY --from=builder /app/target/release/pocket-example-dsql-worker /usr/local/bin/pocket-example-dsql-worker
# pocket.toml は magic_pocket_rs::set_envs() が実行時に読むためコンテナに含める
COPY --from=builder /app/pocket.toml ./pocket.toml

# ENTRYPOINT は使わず CMD のみ (magic-pocket の command 設定との関係)
CMD ["pocket-example-dsql-lambda"]
