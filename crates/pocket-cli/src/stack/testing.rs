//! template vector (`tests/data/template_vectors`) を読むテスト用の補助。

use std::path::{Path, PathBuf};

use serde_json::Value as Json;

pub fn vectors_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/data/template_vectors")
}

pub fn read_text(path: &Path) -> String {
    std::fs::read_to_string(path).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
}

pub fn read_json(path: &Path) -> Json {
    serde_json::from_str(&read_text(path)).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
}

/// manifest.json のあるディレクトリ (入力 × stage) を名前順に返す
pub fn vector_dirs() -> Vec<PathBuf> {
    fn walk(dir: &Path, out: &mut Vec<PathBuf>) {
        if dir.join("manifest.json").is_file() {
            out.push(dir.to_path_buf());
            return;
        }
        for entry in std::fs::read_dir(dir).unwrap_or_else(|e| panic!("{}: {e}", dir.display())) {
            let path = entry.expect("dir entry").path();
            if path.is_dir() {
                walk(&path, out);
            }
        }
    }
    let mut out = Vec::new();
    walk(&vectors_dir(), &mut out);
    out.sort();
    out
}

/// 全 vector の全 stack に `f(ディレクトリ, manifest の stack の要素)` を呼ぶ
pub fn each_stack(mut f: impl FnMut(&Path, &Json)) {
    for dir in vector_dirs() {
        let manifest = read_json(&dir.join("manifest.json"));
        for stack in manifest["stacks"].as_array().expect("stacks") {
            f(&dir, stack);
        }
    }
}
