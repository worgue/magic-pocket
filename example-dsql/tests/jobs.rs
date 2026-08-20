//! pocket.toml の schedule message が worker の Job 型と一致することを検証する。
//!
//! MessageBody は pocket.toml に書いた dict がそのまま JSON 化されたもの。
//! 設定とコードがずれても deploy は通り、**実行時に DLQ へ落ちて初めて気づく**
//! ため、ここで静的に突き合わせる。

use pocket_example_dsql::jobs::Job;

/// pocket.toml の `[<stage>.scheduler.schedules.<name>] message` をすべて取り出す。
fn scheduled_messages() -> Vec<(String, toml::Value)> {
    let doc: toml::Value =
        toml::from_str(&std::fs::read_to_string("pocket.toml").unwrap()).unwrap();
    let mut found = Vec::new();
    // stage テーブル配下 ([sandbox.scheduler.schedules.*]) を走査する
    let toml::Value::Table(root) = &doc else {
        panic!("pocket.toml の最上位がテーブルではありません");
    };
    for (stage, value) in root {
        let Some(schedules) = value
            .get("scheduler")
            .and_then(|s| s.get("schedules"))
            .and_then(|s| s.as_table())
        else {
            continue;
        };
        for (name, entry) in schedules {
            if let Some(message) = entry.get("message") {
                found.push((format!("{stage}.{name}"), message.clone()));
            }
        }
    }
    found
}

#[test]
fn pocket_toml_の_schedule_message_が_job_に_デシリアライズできる() {
    let messages = scheduled_messages();
    assert!(
        !messages.is_empty(),
        "pocket.toml に schedule message が 1 つも見つかりませんでした"
    );
    for (where_, message) in messages {
        let json = serde_json::to_string(&message).unwrap();
        let job: Job = serde_json::from_str(&json)
            .unwrap_or_else(|e| panic!("{where_} の message が Job に一致しません: {e} ({json})"));
        // 本文生成まで通ることを確認する
        assert!(job.body("2026-01-01T00:00:00Z").contains("daily message"));
    }
}

#[test]
fn note_が空なら本文に括弧を付けない() {
    let job = Job::PostMessage {
        note: String::new(),
    };
    assert_eq!(job.body("T"), "daily message at T");
}

#[test]
fn note_があれば本文に付く() {
    let job = Job::PostMessage {
        note: "scheduled".into(),
    };
    assert_eq!(job.body("T"), "daily message at T (scheduled)");
}
