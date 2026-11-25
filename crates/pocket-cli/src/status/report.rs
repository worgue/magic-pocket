//! status の表示 (Python 版 `status_cli.status`)。
//!
//! 判定そのものは [`Evaluator`] に任せ、ここは resource を順に回して行を出すだけ。
//! テストは判定結果を差し替えて、順・文言・出力先・途中停止を確かめる

use anyhow::Result;
use magic_pocket_settings::context::ContainerContext;
use magic_pocket_settings::{mask_secret_values, pyjson};

use super::resource::Resource;
use super::sink::Sink;
use crate::stack::ops::ResourceStatus;

/// resource の状態を調べる側 (本番は AWS / HTTP、テストは固定値)
pub trait Evaluator {
    async fn status(
        &mut self,
        resource: &Resource<'_>,
        sink: &mut dyn Sink,
    ) -> Result<ResourceStatus>;

    /// pocket managed secret がまだ無い store view の数 (`--show-info` の案内に使う)
    async fn secrets_not_ready(&mut self, container: &ContainerContext) -> Result<usize>;

    /// resource の後に出す情報 (DLQ alert の購読状態、inbound の詳細)
    async fn tail(&mut self, sink: &mut dyn Sink) -> Result<()>;
}

pub async fn run(
    resources: &[Resource<'_>],
    evaluator: &mut impl Evaluator,
    show_info: bool,
    sink: &mut dyn Sink,
) -> Result<()> {
    for resource in resources {
        let status = evaluator.status(resource, sink).await?;
        let message = format!("{} status: {status}", resource.class_name());
        match status {
            ResourceStatus::NoExist => sink.info(&message),
            ResourceStatus::RequireUpdate | ResourceStatus::Progress => sink.warning(&message),
            ResourceStatus::Completed => sink.success(&message),
            ResourceStatus::Failed => sink.danger(&message),
        }
        if show_info {
            show_info_message(resource, evaluator, sink).await?;
        }
    }
    evaluator.tail(sink).await
}

async fn show_info_message(
    resource: &Resource<'_>,
    evaluator: &mut impl Evaluator,
    sink: &mut dyn Sink,
) -> Result<()> {
    if let Some(description) = resource.description() {
        sink.info(&description);
    }
    if let Resource::Container(container) = resource {
        for _ in 0..evaluator.secrets_not_ready(container).await? {
            sink.warning(
                "Because pocket managed secrets is not ready yet, the context is not ready to use.",
            );
            sink.warning("Just use it for reference.");
            sink.info("You can create pocket managed secrets by running the below.");
            sink.info("pocket resource container secrets create-pocket-managed");
        }
    }
    // `pocket context` と同じく secret (API key 等) はマスクする
    let masked = mask_secret_values(&resource.context_json());
    sink.print(&pyjson::dumps_indent2(&masked));
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stack::testing::vector_context;
    use crate::status::resource::resources;
    use crate::status::sink::{Recorded, Stream, Style};
    use anyhow::bail;

    /// class 名ごとに決めた結果を返す
    struct Fake {
        results: Vec<(&'static str, Result<ResourceStatus, String>)>,
        not_ready: usize,
        calls: Vec<String>,
    }

    impl Fake {
        fn new(results: Vec<(&'static str, Result<ResourceStatus, String>)>) -> Self {
            Self {
                results,
                not_ready: 0,
                calls: Vec::new(),
            }
        }
    }

    impl Evaluator for Fake {
        async fn status(
            &mut self,
            resource: &Resource<'_>,
            sink: &mut dyn Sink,
        ) -> Result<ResourceStatus> {
            let name = resource.class_name();
            self.calls.push(name.to_string());
            if name == "Vpc" {
                sink.warning("判定中の警告");
            }
            match self.results.iter().find(|(n, _)| *n == name) {
                Some((_, Ok(status))) => Ok(*status),
                Some((_, Err(message))) => bail!("{message}"),
                None => Ok(ResourceStatus::Completed),
            }
        }

        async fn secrets_not_ready(&mut self, _: &ContainerContext) -> Result<usize> {
            Ok(self.not_ready)
        }

        async fn tail(&mut self, sink: &mut dyn Sink) -> Result<()> {
            self.calls.push("tail".to_string());
            sink.print("tail");
            Ok(())
        }
    }

    fn lines(sink: &Recorded) -> Vec<(Stream, Style, &str)> {
        sink.0
            .iter()
            .map(|(s, y, t)| (*s, *y, t.as_str()))
            .collect()
    }

    #[tokio::test]
    async fn test_lines_follow_resource_order_and_status_style() {
        let ctx = vector_context("toml/rds.dev");
        let list = resources(&ctx);
        let names: Vec<&str> = list.iter().map(Resource::class_name).collect();
        assert_eq!(names, ["Vpc", "Rds", "Container"]);
        let mut fake = Fake::new(vec![
            ("Vpc", Ok(ResourceStatus::RequireUpdate)),
            ("Rds", Ok(ResourceStatus::Failed)),
            ("Container", Ok(ResourceStatus::NoExist)),
        ]);
        let mut sink = Recorded::default();
        run(&list, &mut fake, false, &mut sink).await.unwrap();
        assert_eq!(
            lines(&sink),
            [
                (Stream::Stderr, Style::Warning, "判定中の警告"),
                (Stream::Stderr, Style::Warning, "Vpc status: REQUIRE_UPDATE"),
                (Stream::Stderr, Style::Danger, "Rds status: FAILED"),
                (Stream::Stderr, Style::Info, "Container status: NOEXIST"),
                (Stream::Stdout, Style::Plain, "tail"),
            ]
        );
    }

    #[tokio::test]
    async fn test_error_stops_before_later_resources_and_tail() {
        let ctx = vector_context("toml/rds.dev");
        let list = resources(&ctx);
        let mut fake = Fake::new(vec![("Rds", Err("権限がありません".to_string()))]);
        let mut sink = Recorded::default();
        let error = run(&list, &mut fake, false, &mut sink).await.unwrap_err();
        assert_eq!(error.to_string(), "権限がありません");
        assert_eq!(fake.calls, ["Vpc", "Rds"]);
        let texts: Vec<&str> = sink.0.iter().map(|(_, _, t)| t.as_str()).collect();
        assert_eq!(texts, ["判定中の警告", "Vpc status: COMPLETED"]);
    }

    #[tokio::test]
    async fn test_show_info_prints_description_notes_and_masked_json() {
        let ctx = vector_context("toml/rds.dev");
        let list = resources(&ctx);
        let mut fake = Fake::new(vec![]);
        fake.not_ready = 1;
        let mut sink = Recorded::default();
        run(&list, &mut fake, true, &mut sink).await.unwrap();
        let recorded = lines(&sink);
        let at = recorded
            .iter()
            .position(|(_, _, t)| *t == "Container status: COMPLETED")
            .unwrap();
        assert_eq!(recorded[at + 1].1, Style::Info);
        assert!(recorded[at + 1]
            .2
            .starts_with("Create aws cloudformation stack: "));
        assert_eq!(
            recorded[at + 2],
            (
                Stream::Stderr,
                Style::Warning,
                "Because pocket managed secrets is not ready yet, the context is not ready to use."
            )
        );
        assert_eq!(recorded[at + 3].2, "Just use it for reference.");
        assert_eq!(recorded[at + 4].1, Style::Info);
        assert_eq!(
            recorded[at + 5].2,
            "pocket resource container secrets create-pocket-managed"
        );
        // Context の JSON は stdout
        assert_eq!(recorded[at + 6].0, Stream::Stdout);
        assert!(recorded[at + 6].2.starts_with("{\n  \"name\": "));
    }
}
