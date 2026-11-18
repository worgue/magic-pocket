//! deploy の書き込み側の部品 (Python 版 `pocket_cli/resources/` の API 直接操作)。
//!
//! `pocket-rs deploy` に結合するまでは、どのコマンドからも呼ばれない部品がある
//! (テストと、e2e 用の非表示サブコマンドだけが使う)。

// deploy に結合するまで、コマンドから呼ばれない部品がある。結合したら外す
#![allow(dead_code)]

pub mod backup;
pub mod cloudfront;
pub mod container;
pub mod destroy;
pub mod destroy_run;
pub mod dsql;
pub mod ecr;
pub mod edge;
pub mod efs;
pub mod generate;
pub mod iam;
pub mod inbound;
pub mod inbound_domain;
pub mod interaction;
pub mod lambda;
pub mod mediator;
pub mod migrations;
pub mod neon;
pub mod parts;
pub mod pipeline;
pub mod poll;
pub mod provider_urls;
pub mod rds;
pub mod run;
pub mod s3;
pub mod secret_store;
pub mod stacks;
pub mod state;
pub mod stored_value;
pub mod template_secrets;
#[cfg(test)]
pub mod testing;
pub mod tidb;
pub mod upload;
pub mod upstash;
pub mod vpc;
