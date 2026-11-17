//! deploy の書き込み側の部品 (Python 版 `pocket_cli/resources/` の API 直接操作)。
//!
//! `pocket-rs deploy` に結合するまでは、どのコマンドからも呼ばれない部品がある
//! (テストと、e2e 用の非表示サブコマンドだけが使う)。

// deploy に結合するまで、コマンドから呼ばれない部品がある。結合したら外す
#![allow(dead_code)]

pub mod generate;
pub mod iam;
pub mod mediator;
pub mod secret_store;
