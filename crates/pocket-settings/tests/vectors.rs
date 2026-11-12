//! 共有 test vector (`tests/data/settings_vectors`) と Rust の読み込み結果の照合。
//!
//! vector は Python の設定モデルで生成している (`just vectors`)。各 vector の入力
//! toml を純粋層 (`Settings::from_parts`) で読み、`to_json` と secret マスク後の
//! JSON が vector と一致することを確かめる。process の環境は使わない。

use std::collections::BTreeMap;
use std::path::PathBuf;

use magic_pocket_settings::{mask_secret_values, LoadContext, Settings};

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

/// vector の一覧 (tests/data/settings_vectors 配下の .json、相対 path から .json を除いたもの)
fn all_vectors(root: &std::path::Path) -> Vec<String> {
    fn walk(dir: &std::path::Path, base: &std::path::Path, out: &mut Vec<String>) {
        for entry in std::fs::read_dir(dir).expect("vector の dir") {
            let path = entry.expect("dir entry").path();
            if path.is_dir() {
                walk(&path, base, out);
            } else if path.extension().is_some_and(|e| e == "json") {
                let rel = path.strip_prefix(base).expect("相対 path");
                out.push(rel.with_extension("").to_string_lossy().replace('\\', "/"));
            }
        }
    }
    let base = root.join("tests/data/settings_vectors");
    let mut out = Vec::new();
    walk(&base, &base, &mut out);
    out.sort();
    out
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
    }
}

#[test]
fn settings_vectors_match_python() {
    let root = repo_root();
    let names = all_vectors(&root);
    assert!(!names.is_empty());
    let mut failures = Vec::new();
    for name in &names {
        let path = root
            .join("tests/data/settings_vectors")
            .join(format!("{name}.json"));
        let text =
            std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
        let vector: serde_json::Value = serde_json::from_str(&text).expect("vector JSON");
        let input = root.join(vector["input"].as_str().expect("input"));
        let toml_text = std::fs::read_to_string(&input).expect("入力 toml");
        let stage = vector["stage"].as_str().expect("stage");
        let ctx = context(&vector["context"]);
        match Settings::from_parts(&toml_text, stage, &ctx) {
            Ok((settings, _)) => {
                let json = settings.to_json();
                if json != vector["settings"] {
                    failures.push(format!(
                        "{name}: settings が違います\n  rust:   {json}\n  python: {}",
                        vector["settings"]
                    ));
                } else if mask_secret_values(&json) != vector["masked"] {
                    failures.push(format!("{name}: masked が違います"));
                }
            }
            Err(e) => failures.push(format!("{name}: 読み込みに失敗しました: {e}")),
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}
