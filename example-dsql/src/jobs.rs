//! worker が SQS 経由で受け取る Job。
//!
//! MessageBody は pocket.toml の
//! `[<stage>.scheduler.schedules.<name>] message = { ... }` がそのまま JSON 化
//! されたもの。**設定とこの型がずれると実行時まで気づけない**ため、
//! `tests/jobs.rs` が pocket.toml の実値をこの型へデシリアライズして検証する。

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Job {
    /// message を 1 件追加する (削除はしない)
    PostMessage {
        /// 由来を示す任意のメモ。空なら本文に付けない
        #[serde(default)]
        note: String,
    },
}

impl Job {
    /// この Job が作る message 本文。
    pub fn body(&self, now: &str) -> String {
        match self {
            Job::PostMessage { note } if note.is_empty() => format!("daily message at {now}"),
            Job::PostMessage { note } => format!("daily message at {now} ({note})"),
        }
    }
}
