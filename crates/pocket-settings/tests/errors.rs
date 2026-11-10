//! エラーの vector (`tests/data/toml_errors/*.expected.json`) と Rust のエラーの照合。
//!
//! vector は Python の設定モデルで生成している (`just vectors`)。比べ方は kind で決まる:
//! `Value` (独自の検証と前処理) は path と message、`Missing` / `Extra` / `Type` は path
//! だけ、`Syntax` は kind だけ。

use std::collections::BTreeMap;
use std::path::PathBuf;

use magic_pocket_settings::{ErrorKind, LoadContext, PathSeg, Settings};

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn context(v: &serde_json::Value) -> LoadContext {
    let env: BTreeMap<String, String> = v["env"]
        .as_object()
        .expect("context.env")
        .iter()
        .map(|(k, v)| (k.clone(), v.as_str().expect("env の値").to_string()))
        .collect();
    LoadContext {
        project_name_fallback: v["project_name_fallback"]
            .as_str()
            .expect("project_name_fallback")
            .to_string(),
        env,
        runtime_version: v["runtime_version"].as_str().map(str::to_string),
    }
}

fn kind_name(kind: ErrorKind) -> &'static str {
    match kind {
        ErrorKind::Syntax => "Syntax",
        ErrorKind::Missing => "Missing",
        ErrorKind::Extra => "Extra",
        ErrorKind::Type => "Type",
        ErrorKind::Value => "Value",
    }
}

fn path_json(path: &[PathSeg]) -> serde_json::Value {
    serde_json::to_value(path).expect("path")
}

#[test]
fn error_vectors_match_python() {
    let root = repo_root();
    let dir = root.join("tests/data/toml_errors");
    let mut files: Vec<PathBuf> = std::fs::read_dir(&dir)
        .expect("toml_errors")
        .map(|e| e.expect("entry").path())
        .filter(|p| p.to_string_lossy().ends_with(".expected.json"))
        .collect();
    files.sort();
    assert!(!files.is_empty());
    let mut failures = Vec::new();
    for file in &files {
        let name = file
            .file_name()
            .unwrap()
            .to_string_lossy()
            .replace(".expected.json", "");
        let expected: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(file).expect("vector")).expect("JSON");
        let toml_text = std::fs::read_to_string(root.join(expected["input"].as_str().unwrap()))
            .expect("入力 toml");
        let stage = expected["stage"].as_str().unwrap();
        let err = match Settings::from_parts(&toml_text, stage, &context(&expected["context"])) {
            Ok(_) => {
                failures.push(format!("{name}: Rust ではエラーになりません"));
                continue;
            }
            Err(e) => e,
        };
        let kind = expected["kind"].as_str().unwrap();
        if kind_name(err.kind) != kind {
            failures.push(format!(
                "{name}: kind が違います (rust {} / python {kind}): {err}",
                kind_name(err.kind)
            ));
            continue;
        }
        if kind != "Syntax" && path_json(&err.path) != expected["path"] {
            failures.push(format!(
                "{name}: path が違います (rust {} / python {})",
                path_json(&err.path),
                expected["path"]
            ));
            continue;
        }
        if kind == "Value" && err.message != expected["message"].as_str().unwrap() {
            failures.push(format!(
                "{name}: message が違います\n  rust:   {}\n  python: {}",
                err.message, expected["message"]
            ));
        }
    }
    assert!(
        failures.is_empty(),
        "{} 件中 {} 件が違います:\n{}",
        files.len(),
        failures.len(),
        failures.join("\n")
    );
}
