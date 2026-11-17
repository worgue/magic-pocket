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

mod fake {
    //! vector に同梱された外部入力を返す fake (Context の構築用)

    use std::collections::BTreeMap;

    use magic_pocket_settings::context::{Context, ContextInputs, ExternalInputs, HostedZone};
    use magic_pocket_settings::{LoadContext, Settings};
    use serde_json::Value as Json;

    struct FakeExternal<'a>(&'a Json);

    impl ExternalInputs for FakeExternal<'_> {
        fn hosted_zones(&self) -> Result<Vec<HostedZone>, String> {
            Ok(self.0["hosted_zones"]
                .as_array()
                .expect("hosted_zones")
                .iter()
                .map(|z| HostedZone {
                    name: z["Name"].as_str().expect("Name").to_string(),
                    id: z["Id"].as_str().expect("Id").to_string(),
                })
                .collect())
        }

        fn managed_secret_arn(
            &self,
            pocket_key: &str,
            _region: &str,
        ) -> Result<Option<String>, String> {
            Ok(self.0["secret_arns"][pocket_key]
                .as_str()
                .map(str::to_string))
        }

        fn git_short_hash(&self) -> Option<String> {
            self.0["git_short_hash"].as_str().map(str::to_string)
        }

        fn dir_files(&self, dir: &str) -> Option<Vec<String>> {
            let files = self.0["dir_files"][dir].as_array()?;
            Some(
                files
                    .iter()
                    .map(|f| f.as_str().expect("ファイル名").to_string())
                    .collect(),
            )
        }
    }

    /// manifest の入力 (toml × stage) から Context を組む
    pub fn context(repo_root: &std::path::Path, manifest: &Json) -> Context {
        let input = repo_root.join(manifest["input"].as_str().expect("input"));
        let toml_text = super::read_text(&input);
        let stage = manifest["stage"].as_str().expect("stage");
        let env: BTreeMap<String, String> = manifest["context"]["env"]
            .as_object()
            .expect("context.env")
            .iter()
            .map(|(k, v)| (k.clone(), v.as_str().expect("env の値").to_string()))
            .collect();
        let load = LoadContext {
            project_name_fallback: manifest["context"]["project_name_fallback"]
                .as_str()
                .expect("project_name_fallback")
                .to_string(),
            env: env.clone(),
        };
        let (settings, _) = Settings::from_parts(&toml_text, stage, &load)
            .unwrap_or_else(|e| panic!("{}: settings が読めません: {e}", input.display()));
        let external = FakeExternal(&manifest["external"]);
        let inputs = ContextInputs {
            env: &env,
            external: &external,
        };
        let (context, _) = Context::from_settings(&settings, &inputs)
            .unwrap_or_else(|e| panic!("{}: Context を組めません: {e}", input.display()));
        context
    }
}

pub use fake::context;

/// テストの中に書いた pocket.toml から Context を組む (AWS の外部入力は空)
pub fn context_from_toml(toml_text: &str, stage: &str) -> magic_pocket_settings::context::Context {
    let dir = std::env::temp_dir().join(format!(
        "pocket-test-{}-{:?}",
        std::process::id(),
        std::thread::current().id()
    ));
    std::fs::create_dir_all(&dir).expect("一時ディレクトリ");
    let input = dir.join("pocket.toml");
    std::fs::write(&input, toml_text).expect("pocket.toml");
    let manifest = serde_json::json!({
        "input": input,
        "stage": stage,
        "context": {"env": {}, "project_name_fallback": "testprj"},
        "external": {"hosted_zones": [], "secret_arns": {}, "dir_files": {}},
    });
    let context = context(std::path::Path::new("/"), &manifest);
    std::fs::remove_dir_all(&dir).ok();
    context
}

pub fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

/// vector (`<group>/<名前>.<stage>`) の入力から Context を組む
pub fn vector_context(vector: &str) -> magic_pocket_settings::context::Context {
    let manifest = read_json(&vectors_dir().join(vector).join("manifest.json"));
    context(&repo_root(), &manifest)
}
