//! pocket.toml の読み込み。前処理は Python の `Settings.from_toml` と同じ順:
//! generator version → top-level key → stage → stage 上書きの merge → stage
//! table の除去 → `stage` の注入 → 廃止キーの案内 → deserialize → validator。

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use crate::error::{from_serde, path, SettingsError};
use crate::model::{Settings, TOP_LEVEL_KEYS};
use crate::types::mark_datetimes;

/// runtime.toml の生成元 (CLI) の版を示すコメント (Python の `GENERATOR_VERSION_MARKER`)
const GENERATOR_VERSION_MARKER: &str = "# magic-pocket-cli generator version:";

/// 読み込みに使うファイル以外の入力。純粋層 (`Settings::from_parts`) はこれだけを見る
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LoadContext {
    /// `[general] project_name` を省略したときの値 (Python では pyproject.toml の
    /// `project.name`、無ければ cwd 名)
    pub project_name_fallback: String,
    /// 環境変数と `.env` を合わせたもの (`[neon]` 等の credential 用)
    pub env: BTreeMap<String, String>,
    /// 読み手の runtime の版。runtime.toml の生成元がこれより新しければ止める。
    /// None なら照合しない
    pub runtime_version: Option<String>,
}

/// 検証は通ったが知らせたいこと (Python では stderr に出す警告)
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Advisory {
    pub message: String,
}

impl Settings {
    /// ファイルと環境 (cwd・環境変数) から読む。
    pub fn load(toml_path: &Path, stage: &str) -> Result<(Settings, Vec<Advisory>), SettingsError> {
        let text = std::fs::read_to_string(toml_path).map_err(|e| {
            SettingsError::value(
                Vec::new(),
                format!("{} を読めません: {e}", toml_path.display()),
            )
        })?;
        let cwd = std::env::current_dir()
            .map_err(|e| SettingsError::value(Vec::new(), format!("cwd を取得できません: {e}")))?;
        let ctx = LoadContext {
            project_name_fallback: project_name_fallback(&cwd),
            env: std::env::vars().collect(),
            runtime_version: None,
        };
        Settings::from_parts(&text, stage, &ctx)
    }

    /// toml の本文と明示の入力だけから読む (ファイル・環境を見ない)。
    pub fn from_parts(
        toml_text: &str,
        stage: &str,
        ctx: &LoadContext,
    ) -> Result<(Settings, Vec<Advisory>), SettingsError> {
        check_generator_version(toml_text, ctx.runtime_version.as_deref())?;
        let mut data: toml::Table =
            toml::from_str(toml_text).map_err(|e| SettingsError::syntax(e.to_string()))?;
        let stages = stages(&data)?;
        check_keys(&data, &stages)?;
        check_stage(stage, &stages)?;
        if let Some(toml::Value::Table(over)) = data.get(stage).cloned() {
            deep_merge(&mut data, &over);
        }
        for s in &stages {
            data.remove(s);
        }
        data.insert("stage".to_string(), toml::Value::String(stage.to_string()));
        fill_project_name(&mut data, &ctx.project_name_fallback);
        resolve_vpc(&mut data)?;
        accept_build_string(&mut data);
        reject_legacy_keys(&data)?;
        let mut data = toml::Value::Table(data);
        mark_datetimes(&mut data);
        let mut settings: Settings = serde_path_to_error::deserialize(data).map_err(from_serde)?;
        settings.validate()?;
        Ok((settings, Vec::new()))
    }
}

/// pyproject.toml を cwd から上方向に探し、`project.name` を返す。
/// 無ければ cwd 名 (Python の `get_project_name`)
fn project_name_fallback(cwd: &Path) -> String {
    let cwd_name = || {
        cwd.file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default()
    };
    let Some(pyproject) = find_upward(cwd, "pyproject.toml") else {
        return cwd_name();
    };
    let name = std::fs::read_to_string(pyproject)
        .ok()
        .and_then(|text| text.parse::<toml::Table>().ok())
        .and_then(|t| {
            t.get("project")?
                .get("name")?
                .as_str()
                .filter(|s| !s.is_empty())
                .map(str::to_string)
        });
    name.unwrap_or_else(cwd_name)
}

fn find_upward(start: &Path, name: &str) -> Option<PathBuf> {
    start
        .ancestors()
        .map(|dir| dir.join(name))
        .find(|p| p.is_file())
}

/// Python の `version_tuple`: 各パートの先頭の数字だけを採る
fn version_tuple(version: &str) -> Vec<u64> {
    version
        .split('.')
        .map(|part| {
            let digits: String = part.chars().take_while(|c| c.is_ascii_digit()).collect();
            digits.parse().unwrap_or(0)
        })
        .collect()
}

fn check_generator_version(text: &str, runtime: Option<&str>) -> Result<(), SettingsError> {
    let Some(runtime) = runtime else {
        return Ok(());
    };
    let generator = text.lines().find_map(|line| {
        let rest = line.trim().strip_prefix(GENERATOR_VERSION_MARKER)?.trim();
        (!rest.is_empty()).then(|| rest.to_string())
    });
    let Some(generator) = generator else {
        return Ok(());
    };
    if version_tuple(&generator) > version_tuple(runtime) {
        return Err(SettingsError::value(
            Vec::new(),
            format!(
                "pocket.runtime.toml は magic-pocket-cli {generator} が生成しましたが、この \
                 runtime (magic-pocket) は {runtime} です。新しい pocket.toml 機能を古い \
                 runtime が解釈できず Lambda init が失敗します。上げてください: \
                 uv add 'magic-pocket[django]>={generator}'"
            ),
        ));
    }
    Ok(())
}

/// `[general] stages`。前処理 (key・stage の検査) がこれを使うので先に取り出す
fn stages(data: &toml::Table) -> Result<Vec<String>, SettingsError> {
    let general = match data.get("general") {
        None => {
            return Err(SettingsError {
                path: path(&["general"]),
                kind: crate::ErrorKind::Missing,
                message: "Field required".to_string(),
            })
        }
        Some(toml::Value::Table(t)) => t,
        Some(_) => {
            return Err(SettingsError {
                path: path(&["general"]),
                kind: crate::ErrorKind::Type,
                message: "Input should be a valid dictionary".to_string(),
            })
        }
    };
    let type_error = || SettingsError {
        path: path(&["general", "stages"]),
        kind: crate::ErrorKind::Type,
        message: "Input should be a valid list of strings".to_string(),
    };
    match general.get("stages") {
        None => Err(SettingsError {
            path: path(&["general", "stages"]),
            kind: crate::ErrorKind::Missing,
            message: "Field required".to_string(),
        }),
        Some(toml::Value::Array(a)) => a
            .iter()
            .map(|v| v.as_str().map(str::to_string).ok_or_else(type_error))
            .collect(),
        Some(_) => Err(type_error()),
    }
}

fn check_keys(data: &toml::Table, stages: &[String]) -> Result<(), SettingsError> {
    for key in data.keys() {
        // 旧 [awscontainer] は reject_legacy_keys の移行案内に任せる
        let valid = TOP_LEVEL_KEYS.contains(&key.as_str())
            || stages.iter().any(|s| s == key)
            || key == "awscontainer";
        if !valid {
            return Err(SettingsError::value(
                Vec::new(),
                format!(
                    "invalid key {key} in pocket.toml\nIf it's a stage name, add it to stages."
                ),
            ));
        }
    }
    Ok(())
}

fn check_stage(stage: &str, stages: &[String]) -> Result<(), SettingsError> {
    if stages.iter().any(|s| s == stage) {
        return Ok(());
    }
    let listed: Vec<String> = stages.iter().map(|s| format!("'{s}'")).collect();
    Err(SettingsError::value(
        Vec::new(),
        format!(
            "ステージ '{stage}' は pocket.toml に定義されていません。\n  定義済みステージ: [{}]\n  \
             --stage オプションまたは POCKET_DEPLOY_STAGE 環境変数を確認してください。",
            listed.join(", ")
        ),
    ))
}

/// table 同士は再帰で merge、それ以外 (配列・スカラー) は置き換える
fn deep_merge(base: &mut toml::Table, over: &toml::Table) {
    for (key, value) in over {
        match (base.get_mut(key), value) {
            (Some(toml::Value::Table(b)), toml::Value::Table(o)) => deep_merge(b, o),
            _ => {
                base.insert(key.clone(), value.clone());
            }
        }
    }
}

/// `[general] project_name` の既定 (Python の `default_factory=get_project_name`)
fn fill_project_name(data: &mut toml::Table, fallback: &str) {
    if let Some(toml::Value::Table(general)) = data.get_mut("general") {
        general
            .entry("project_name")
            .or_insert_with(|| toml::Value::String(fallback.to_string()));
    }
}

/// `use_vpc` から `container.<name>.vpc` と `rds.vpc` を決める (Python の `resolve_vpc`)。
/// 未指定は `[vpc]` があれば使う、true は `[vpc]` 必須、それ以外は使わない
fn resolve_vpc(data: &mut toml::Table) -> Result<(), SettingsError> {
    let vpc = data
        .get("vpc")
        .filter(|v| !matches!(v, toml::Value::Table(t) if t.is_empty()))
        .cloned();
    let mut sections: Vec<(String, &mut toml::Table)> = Vec::new();
    let (containers, rest): (Vec<_>, Vec<_>) = data
        .iter_mut()
        .filter(|(k, _)| *k == "container" || *k == "rds")
        .partition(|(k, _)| *k == "container");
    for (_, value) in containers {
        if let toml::Value::Table(containers) = value {
            for (name, c) in containers.iter_mut() {
                if let toml::Value::Table(c) = c {
                    sections.push((format!("container.{name}"), c));
                }
            }
        }
    }
    for (_, value) in rest {
        if let toml::Value::Table(rds) = value {
            sections.push(("rds".to_string(), rds));
        }
    }
    for (label, section) in sections {
        match section.remove("use_vpc") {
            None => {
                if let Some(vpc) = &vpc {
                    section.insert("vpc".to_string(), vpc.clone());
                }
            }
            Some(toml::Value::Boolean(true)) => {
                let Some(vpc) = &vpc else {
                    return Err(SettingsError::value(
                        Vec::new(),
                        format!("{label}.use_vpc=true ですが [vpc] が定義されていません"),
                    ));
                };
                section.insert("vpc".to_string(), vpc.clone());
            }
            // false (と true 以外の値) は VPC を使わない
            Some(_) => {}
        }
    }
    Ok(())
}

/// `build = "docker"` を `{ backend = "docker" }` と同じに読む (Python の
/// `BuildConfig.accept_string`)
fn accept_build_string(data: &mut toml::Table) {
    let Some(toml::Value::Table(containers)) = data.get_mut("container") else {
        return;
    };
    for (_, c) in containers.iter_mut() {
        let toml::Value::Table(c) = c else { continue };
        if let Some(toml::Value::String(backend)) = c.get("build").cloned() {
            let mut t = toml::Table::new();
            t.insert("backend".to_string(), toml::Value::String(backend));
            c.insert("build".to_string(), toml::Value::Table(t));
        }
    }
}

/// 廃止したキーを移行案内つきで拒否する (Python の `mode="before"` validator)。
/// 未知キーとして弾くより先に見る
fn reject_legacy_keys(data: &toml::Table) -> Result<(), SettingsError> {
    if data.contains_key("awscontainer") {
        return Err(SettingsError::value(
            Vec::new(),
            "[awscontainer] は廃止されました。[container.<name>] の dict 形式に\
             移行してください (container は複数宣言できます)。\n  \
             例) [awscontainer] → [container.mydjango]\n      \
             [awscontainer.handlers.wsgi] → [container.mydjango.handlers.wsgi]\n      \
             [sandbox.awscontainer] → [sandbox.container.mydjango]\n  \
             あわせて cloudfront routes / scheduler の handler 参照を \
             \"<container>.<handler>\" のドット記法に変更してください \
             (例: handler = \"mydjango.wsgi\")。移行手順は CHANGELOG 0.29.0 を参照。",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::PathSeg;

    #[test]
    fn test_project_name_fallback_reads_pyproject_upward() {
        let dir = std::env::temp_dir().join(format!("pocket-settings-{}", std::process::id()));
        let sub = dir.join("a/b");
        std::fs::create_dir_all(&sub).unwrap();
        std::fs::write(
            dir.join("pyproject.toml"),
            "[project]\nname = \"from-pyproject\"\n",
        )
        .unwrap();
        assert_eq!(project_name_fallback(&sub), "from-pyproject");
        std::fs::write(dir.join("pyproject.toml"), "[tool.x]\n").unwrap();
        assert_eq!(project_name_fallback(&sub), "b");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    fn load_err(toml_text: &str, stage: &str) -> SettingsError {
        let ctx = LoadContext {
            project_name_fallback: "prj".to_string(),
            ..Default::default()
        };
        Settings::from_parts(toml_text, stage, &ctx).unwrap_err()
    }

    const BASE: &str = "[general]\nregion = \"r\"\nstages = [\"dev\"]\n";

    #[test]
    fn test_error_path_and_kind() {
        use crate::ErrorKind::*;
        let cases: &[(&str, &str, Vec<PathSeg>, crate::ErrorKind)] = &[
            ("[general]\nstages = [\"dev\"]\n", "dev", path(&["general", "region"]), Missing),
            ("[general]\nregion = \"r\"\nstages = [\"dev\"]\nfoo = 1\n", "dev", path(&["general", "foo"]), Extra),
            ("[general]\nregion = 5\nstages = [\"dev\"]\n", "dev", path(&["general", "region"]), Type),
            ("[general]\nregion = 1979-05-27\nstages = [\"dev\"]\n", "dev", path(&["general", "region"]), Type),
            ("[general]\nregion = \"r\"\nstages = [\"Dev\"]\n", "Dev", path(&["stage"]), Type),
            (&format!("{BASE}[iam]\nexternal_roles = \"yes\"\n"), "dev", path(&["iam", "external_roles"]), Type),
            (
                &format!("{BASE}[general.django_fallback.storages]\nstaticfiles = {{ store = \"filesystem\" }}\n"),
                "dev",
                path(&["general", "django_fallback"]),
                Value,
            ),
            (
                &format!("{BASE}[general.django_fallback.storages.default]\nstore = \"s3\"\n"),
                "dev",
                path(&["general", "django_fallback", "storages", "default"]),
                Value,
            ),
            ("[general]\nregion = \"r\"\nstages = [\"dev\"]\n", "prod", Vec::new(), Value),
            (&format!("{BASE}[foo]\n"), "dev", Vec::new(), Value),
            ("[general\n", "dev", Vec::new(), Syntax),
            (&format!("{BASE}[vpc]\nref = \"x\"\n"), "dev", path(&["vpc"]), Value),
            (&format!("{BASE}[vpc]\nref = \"x\"\nzone_suffixes = [\"ab\"]\n"), "dev", vec![PathSeg::Key("vpc".into()), PathSeg::Key("zone_suffixes".into()), PathSeg::Index(0)], Type),
            (&format!("{BASE}[vpc]\nref = \"x\"\nmanage = false\nefs = {{ local_mount_path = \"/tmp\" }}\n"), "dev", path(&["vpc", "efs", "local_mount_path"]), Type),
            (&format!("{BASE}[s3]\nbucket_name_format = \"Bad\"\n"), "dev", path(&["s3", "bucket_name_format"]), Type),
            (&format!("{BASE}[s3]\nlifecycle_rules = [{{ id = \"a\", prefix = \"\", expiration_days = 0 }}]\n"), "dev", vec![PathSeg::Key("s3".into()), PathSeg::Key("lifecycle_rules".into()), PathSeg::Index(0), PathSeg::Key("expiration_days".into())], Type),
            (&format!("{BASE}[s3]\nlifecycle_rules = [{{ id = \"a\", prefix = \"\" }}]\n"), "dev", vec![PathSeg::Key("s3".into()), PathSeg::Key("lifecycle_rules".into()), PathSeg::Index(0)], Value),
            (&format!("{BASE}[container.a]\nuse_vpc = true\n"), "dev", Vec::new(), Value),
            (&format!("{BASE}[container.Bad]\ndockerfile_path = \"D\"\n"), "dev", Vec::new(), Value),
            (&format!("{BASE}[container.a]\ndockerfile_path = \"D\"\nplatform = \"x\"\n"), "dev", path(&["container", "a", "platform"]), Type),
            (&format!("{BASE}[container.a]\ndockerfile_path = \"D\"\nhandlers = {{ w = {{ command = \"c\", sqs = {{}} }} }}\n"), "dev", path(&["container", "a", "handlers", "w", "sqs"]), Value),
            (&format!("{BASE}[container.a]\ndockerfile_path = \"D\"\nhandlers = {{ w = {{ command = \"c\", timeout = \"30\" }} }}\n"), "dev", path(&["container", "a", "handlers", "w", "timeout"]), Type),
            (&format!("{BASE}[container.a]\ndockerfile_path = \"D\"\nsecrets = {{ managed = {{ \"bad-key\" = {{ type = \"password\" }} }} }}\n"), "dev", path(&["container", "a", "secrets", "managed", "bad-key", "[key]"]), Type),
            (&format!("{BASE}[container.a]\ndockerfile_path = \"D\"\nsecrets = {{ managed = {{ K = {{ type = \"password\", options = {{ c = true }} }} }} }}\n"), "dev", path(&["container", "a", "secrets", "managed", "K", "options", "c"]), Type),
            (&format!("{BASE}[container.a]\ndockerfile_path = \"D\"\nsecrets = {{ user = {{ K = {{ name = \"n\", type = \"neon_database_url\" }} }} }}\n"), "dev", path(&["container", "a", "secrets"]), Value),
            (&format!("{BASE}[container.a]\ndockerfile_path = \"D\"\nsecrets = {{ store = \"ssm\" }}\n[container.b]\ndockerfile_path = \"D\"\nsecrets = {{}}\n"), "dev", Vec::new(), Value),
        ];
        for (text, stage, want_path, want_kind) in cases {
            let e = load_err(text, stage);
            assert_eq!((&e.path, e.kind), (want_path, *want_kind), "{text}: {e}");
        }
    }

    #[test]
    fn test_version_tuple_matches_python() {
        assert_eq!(version_tuple("0.10.0"), vec![0, 10, 0]);
        assert_eq!(version_tuple("1.2.3rc1"), vec![1, 2, 3]);
        assert!(version_tuple("0.10.0") > version_tuple("0.9.9"));
    }
}
