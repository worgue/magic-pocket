//! `pocket-rs status`: deploy が管理する resource の状態を一覧で出す (Python 版 `pocket status`)。
//!
//! - [`resource`]: resource の並び (deploy と同じ順)
//! - [`report`]: 表示 (順・文言・出力先)。判定は [`report::Evaluator`] 越しに呼ぶ
//! - [`aws`]: AWS / HTTP から状態を取る本番の Evaluator
//! - それ以外: resource ごとの判定 (取得した事実から status を決める純関数)

mod alerts;
mod aws;
pub(crate) mod container;
mod dsql;
mod inbound;
mod providers;
mod rds;
mod report;
mod resource;
mod s3;
pub(crate) mod sink;

use anyhow::Result;

use crate::context::load_context_read_only;

pub async fn run(stage: &str, show_info: bool) -> Result<()> {
    let (context, _toml_dir) = load_context_read_only(stage)?;
    let sdk = aws_config::load_from_env().await;
    let resources = resource::resources(&context);
    let mut evaluator = aws::AwsEvaluator::new(&context, sdk);
    let mut sink = sink::Terminal::new();
    report::run(&resources, &mut evaluator, show_info, &mut sink).await
}
