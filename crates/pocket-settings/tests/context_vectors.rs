//! 共有 test vector (`tests/data/context_vectors`) と Rust の Context の照合。
//!
//! vector は Python の `Context.from_settings` で生成している (`just vectors`)。
//! 外部入力 (hosted zone / managed secret の ARN / git の short hash / managed_assets
//! のディレクトリ) は vector に同梱された値を fake で注入する。JSON の一致に加えて、
//! マスク後の `json.dumps(indent=2)` の文字列 (`pocket context` の stdout) を key の
//! 順まで比べる。

use std::cell::RefCell;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use magic_pocket_settings::context::{Context, ContextInputs, ExternalInputs, HostedZone};
use magic_pocket_settings::{mask_secret_values, pyjson, LoadContext, Settings};

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn all_vectors(base: &Path) -> Vec<PathBuf> {
    fn walk(dir: &Path, out: &mut Vec<PathBuf>) {
        for entry in std::fs::read_dir(dir).expect("vector の dir") {
            let path = entry.expect("dir entry").path();
            if path.is_dir() {
                walk(&path, out);
            } else if path.extension().is_some_and(|e| e == "json") {
                out.push(path);
            }
        }
    }
    let mut out = Vec::new();
    walk(base, &mut out);
    out.sort();
    out
}

/// vector の `external` を返す fake。Python が引いていない値を Rust が引いたら記録する
struct FakeExternal {
    zones: Vec<HostedZone>,
    git_short_hash: String,
    secret_arns: BTreeMap<String, Option<String>>,
    dir_files: BTreeMap<String, Vec<String>>,
    unexpected: RefCell<Vec<String>>,
}

impl FakeExternal {
    fn from_vector(v: &serde_json::Value) -> Self {
        let zones = v["hosted_zones"]
            .as_array()
            .expect("hosted_zones")
            .iter()
            .map(|z| HostedZone {
                name: z["Name"].as_str().expect("Name").to_string(),
                id: z["Id"].as_str().expect("Id").to_string(),
            })
            .collect();
        let secret_arns = v["secret_arns"]
            .as_object()
            .expect("secret_arns")
            .iter()
            .map(|(k, v)| (k.clone(), v.as_str().map(str::to_string)))
            .collect();
        let dir_files = v["dir_files"]
            .as_object()
            .expect("dir_files")
            .iter()
            .map(|(k, v)| {
                let files = v
                    .as_array()
                    .expect("dir_files の値")
                    .iter()
                    .map(|f| f.as_str().expect("ファイル名").to_string())
                    .collect();
                (k.clone(), files)
            })
            .collect();
        Self {
            zones,
            git_short_hash: v["git_short_hash"]
                .as_str()
                .expect("git_short_hash")
                .to_string(),
            secret_arns,
            dir_files,
            unexpected: RefCell::new(Vec::new()),
        }
    }
}

impl ExternalInputs for FakeExternal {
    fn hosted_zones(&self) -> Result<Vec<HostedZone>, String> {
        Ok(self.zones.clone())
    }

    fn managed_secret_arn(
        &self,
        pocket_key: &str,
        _region: &str,
    ) -> Result<Option<String>, String> {
        match self.secret_arns.get(pocket_key) {
            Some(arn) => Ok(arn.clone()),
            None => {
                self.unexpected
                    .borrow_mut()
                    .push(format!("managed_secret_arn({pocket_key})"));
                Ok(None)
            }
        }
    }

    fn git_short_hash(&self) -> Option<String> {
        Some(self.git_short_hash.clone())
    }

    fn dir_files(&self, dir: &str) -> Option<Vec<String>> {
        self.dir_files.get(dir).cloned()
    }
}

fn load_context(v: &serde_json::Value) -> LoadContext {
    LoadContext {
        project_name_fallback: v["project_name_fallback"]
            .as_str()
            .expect("project_name_fallback")
            .to_string(),
    }
}

fn env_map(v: &serde_json::Value) -> BTreeMap<String, String> {
    v["env"]
        .as_object()
        .expect("context.env")
        .iter()
        .map(|(k, v)| (k.clone(), v.as_str().expect("env の値").to_string()))
        .collect()
}

/// 1 件の vector を照合し、違えば理由を返す
fn check_vector(root: &Path, path: &Path) -> Option<String> {
    let text = std::fs::read_to_string(path).expect("vector");
    let vector: serde_json::Value = serde_json::from_str(&text).expect("vector JSON");
    let input = root.join(vector["input"].as_str().expect("input"));
    let toml_text = std::fs::read_to_string(&input).expect("入力 toml");
    let stage = vector["stage"].as_str().expect("stage");
    let (settings, _) = Settings::from_parts(&toml_text, stage, &load_context(&vector["context"]))
        .unwrap_or_else(|e| panic!("{}: settings が読めません: {e}", path.display()));
    let env = env_map(&vector["context"]);
    let external = FakeExternal::from_vector(&vector["external"]);
    let inputs = ContextInputs {
        env: &env,
        external: &external,
    };
    let result = Context::from_settings(&settings, &inputs);
    if let Some(unexpected) = external.unexpected.borrow().first() {
        return Some(format!(
            "Python が呼ばない外部入力を呼びました: {unexpected}"
        ));
    }
    match (result, vector.get("error")) {
        (Ok((ctx, _)), None) => {
            let json = ctx.to_json();
            if json != vector["result"] {
                return Some(format!(
                    "Context が違います\n  rust:   {json}\n  python: {}",
                    vector["result"]
                ));
            }
            let masked = pyjson::dumps_indent2(&mask_secret_values(&json));
            if masked != vector["masked_text"].as_str().expect("masked_text") {
                return Some("マスク後の JSON 文字列 (key の順) が違います".to_string());
            }
            None
        }
        (Err(e), Some(want)) if want.as_str() == Some(e.0.as_str()) => None,
        (Err(e), want) => Some(format!(
            "エラーが違います\n  rust:   {e}\n  python: {want:?}"
        )),
        (Ok(_), Some(want)) => Some(format!("エラーになりません (python: {want})")),
    }
}

#[test]
fn context_vectors_match_python() {
    let root = repo_root();
    let paths = all_vectors(&root.join("tests/data/context_vectors"));
    assert!(!paths.is_empty());
    let failures: Vec<String> = paths
        .iter()
        .filter_map(|p| check_vector(&root, p).map(|m| format!("{}: {m}", p.display())))
        .collect();
    assert!(
        failures.is_empty(),
        "{} / {} 件が一致しません:\n{}",
        failures.len(),
        paths.len(),
        failures.join("\n")
    );
}
