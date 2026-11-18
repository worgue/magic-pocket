//! build context (= pocket.toml のディレクトリ) のファイル列挙と permission 検査。
//!
//! Python 版 `pocket_cli/resources/aws/builders/dockerignore.py` と `context_check.py`
//! の移植。列挙は `.dockerignore` (無ければ既定の除外) を gitignore 風に当てる。
//! CodeBuild backend の source zip と、docker / depot backend の other-read 検査が使う。

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use anyhow::{bail, Context as _, Result};
use ignore::gitignore::{Gitignore, GitignoreBuilder};

/// `.dockerignore` が無いときの除外
pub const DEFAULT_EXCLUDES: &[&str] = &[
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "*.pyc",
    ".env",
    ".forge",
];

/// 一覧に載せる件数の上限 (超えた分は件数だけ出す)
const MAX_LISTED: usize = 10;

/// runtime が INIT で読む設定ファイル。other-read が無いと確実に INIT で失敗するため、
/// 警告でなくエラーにする
const INIT_CRITICAL_NAMES: &[&str] = &["pocket.toml"];

pub struct IgnoreSpec {
    matcher: Gitignore,
    /// `!` (再包含) の pattern があるか。あるとディレクトリの枝刈りができない
    has_negation: bool,
}

impl IgnoreSpec {
    fn excluded(&self, rel: &str, is_dir: bool) -> bool {
        self.matcher
            .matched_path_or_any_parents(Path::new(rel), is_dir)
            .is_ignore()
    }
}

/// context_dir の `.dockerignore` を読む (無ければ [`DEFAULT_EXCLUDES`])
pub fn load_dockerignore(context_dir: &Path) -> Result<IgnoreSpec> {
    let path = context_dir.join(".dockerignore");
    let lines: Vec<String> = if path.exists() {
        fs::read_to_string(&path)
            .with_context(|| format!("{} を読めません", path.display()))?
            .lines()
            .map(str::trim)
            .filter(|l| !l.is_empty() && !l.starts_with('#'))
            .map(str::to_string)
            .collect()
    } else {
        DEFAULT_EXCLUDES.iter().map(|s| s.to_string()).collect()
    };
    let mut builder = GitignoreBuilder::new(context_dir);
    for line in &lines {
        builder
            .add_line(None, line)
            .with_context(|| format!(".dockerignore の pattern {line:?} を読めません"))?;
    }
    Ok(IgnoreSpec {
        matcher: builder.build()?,
        has_negation: lines.iter().any(|l| l.starts_with('!')),
    })
}

/// 列挙した 1 ファイル
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SourceFile {
    pub abs: PathBuf,
    /// context_dir からの相対パス (`/` 区切り)
    pub rel: String,
}

impl SourceFile {
    /// 実体の無い symlink か (stat できないので zip / 検査の対象外)
    pub fn is_broken_symlink(&self) -> bool {
        !self.abs.exists()
    }
}

/// dockerignore 適用済みのファイルを決定的な順 (名前順の深さ優先) で列挙する。
///
/// Python の `os.walk` (symlink のディレクトリは辿らない) と同じく、ディレクトリへの
/// symlink は列挙しない。ファイルへの symlink と壊れた symlink はファイルとして載る。
/// `!` が無いときは除外ディレクトリを枝刈りし、あるときは配下のファイルを 1 つずつ判定する
/// (除外ディレクトリ配下の再包含に届くようにする = docker CLI と同じ)
pub fn iter_source_files(context_dir: &Path, spec: &IgnoreSpec) -> Result<Vec<SourceFile>> {
    let mut files = Vec::new();
    walk(context_dir, "", spec, &mut files)?;
    Ok(files)
}

fn walk(dir: &Path, rel_dir: &str, spec: &IgnoreSpec, out: &mut Vec<SourceFile>) -> Result<()> {
    let mut dirs: Vec<(String, PathBuf)> = Vec::new();
    let mut names: Vec<(String, PathBuf)> = Vec::new();
    for entry in fs::read_dir(dir).with_context(|| format!("{} を読めません", dir.display()))?
    {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().into_owned();
        let path = entry.path();
        let file_type = entry.file_type()?;
        if file_type.is_dir() {
            dirs.push((name, path));
        } else if file_type.is_symlink() && path.is_dir() {
            // os.walk ではディレクトリ扱い (dirnames) だが辿らない
        } else {
            names.push((name, path));
        }
    }
    dirs.sort();
    names.sort();
    let join = |name: &str| {
        if rel_dir.is_empty() {
            name.to_string()
        } else {
            format!("{rel_dir}/{name}")
        }
    };
    for (name, path) in names {
        let rel = join(&name);
        if !spec.excluded(&rel, false) {
            out.push(SourceFile { abs: path, rel });
        }
    }
    for (name, path) in dirs {
        let rel = join(&name);
        if !spec.has_negation && spec.excluded(&rel, true) {
            continue;
        }
        walk(&path, &rel, spec, out)?;
    }
    Ok(())
}

/// other-read の無いファイル ("path (mode)" の 8 進表記)。壊れた symlink は対象外
pub fn find_files_without_world_read(context_dir: &Path) -> Result<Vec<(String, u32)>> {
    let spec = load_dockerignore(context_dir)?;
    let mut found = Vec::new();
    for file in iter_source_files(context_dir, &spec)? {
        if file.is_broken_symlink() {
            continue;
        }
        let mode = fs::metadata(&file.abs)?.permissions().mode();
        if mode & 0o004 == 0 {
            found.push((file.rel, mode & 0o777));
        }
    }
    Ok(found)
}

/// この process の build で警告した other-read の無いファイル ("path (mode)")
static WARNED_FILES: std::sync::Mutex<Vec<String>> = std::sync::Mutex::new(Vec::new());

/// build のときの other-read の警告を、deploy の最後にもう一度出す文言。
///
/// build のログの途中の警告は大量の出力に埋もれて気付きにくい。警告が無ければ `None`
pub fn world_read_warning_summary() -> Option<String> {
    let warned = WARNED_FILES.lock().unwrap_or_else(|e| e.into_inner());
    summarize_warned(&warned)
}

fn summarize_warned(warned: &[String]) -> Option<String> {
    if warned.is_empty() {
        return None;
    }
    let mut listed = warned
        .iter()
        .take(MAX_LISTED)
        .cloned()
        .collect::<Vec<_>>()
        .join(", ");
    if warned.len() > MAX_LISTED {
        listed += &format!(" (他 {} 件)", warned.len() - MAX_LISTED);
    }
    Some(format!(
        "注意: build context に other-read の無いファイルがありました ({} 件): \
         {listed}。Dockerfile で `COPY --chmod` 等により正規化していない場合、Lambda の\
         非 root 実行ユーザーが読めず INIT フェーズで失敗します。\
         `chmod 644 <file>` で修正してください。",
        warned.len()
    ))
}

/// docker / depot backend の build 前の検査 (context は生の permission のまま image に入る)。
///
/// 一般のファイルは警告だけにする (Dockerfile の `COPY --chmod` 等で正規化している構成が
/// あるため)。pocket.toml は該当 container が確実に INIT で死ぬのでエラー
pub fn warn_files_without_world_read(context_dir: &Path) -> Result<()> {
    let unreadable = find_files_without_world_read(context_dir)?;
    if unreadable.is_empty() {
        return Ok(());
    }
    let mut listed = unreadable
        .iter()
        .take(MAX_LISTED)
        .map(|(rel, mode)| format!("{rel} ({mode:o})"))
        .collect::<Vec<_>>()
        .join(", ");
    if unreadable.len() > MAX_LISTED {
        listed += &format!(" (他 {} 件)", unreadable.len() - MAX_LISTED);
    }
    {
        let mut warned = WARNED_FILES.lock().unwrap_or_else(|e| e.into_inner());
        for (rel, mode) in &unreadable {
            let entry = format!("{rel} ({mode:o})");
            if !warned.contains(&entry) {
                warned.push(entry);
            }
        }
    }
    eprintln!(
        "build context に other-read の無いファイルがあります ({} 件): {listed}。\
         Lambda の実行ユーザーは非 root のため、このまま image に COPY されると\
         読めず INIT フェーズで失敗します (wsgi も同じ image のためサイトごと \
         500)。`chmod 644 <file>` で修正するか、Dockerfile の COPY に \
         `--chmod` を付けて build 段で正規化してください。",
        unreadable.len()
    );
    let critical: Vec<&str> = unreadable
        .iter()
        .map(|(rel, _)| rel.as_str())
        .filter(|rel| {
            let name = Path::new(rel).file_name().and_then(|n| n.to_str());
            name.is_some_and(|n| INIT_CRITICAL_NAMES.contains(&n))
        })
        .collect();
    if !critical.is_empty() {
        bail!(
            "build context の {} に other-read がありません。runtime が INIT \
             フェーズで読む設定ファイルのため、このまま image に COPY されると\
             該当 container は確実に INIT で失敗します (PermissionDenied)。\
             `chmod 644 <file>` で修正してください (COPY --chmod で image 内を\
             正規化している場合も、local 側を 644 にして問題ありません)。\
             image に COPY しない場合は .dockerignore に追加してください。",
            critical.join(", ")
        );
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::symlink;

    use super::*;

    fn rels(dir: &Path) -> Vec<String> {
        let spec = load_dockerignore(dir).unwrap();
        iter_source_files(dir, &spec)
            .unwrap()
            .into_iter()
            .map(|f| f.rel)
            .collect()
    }

    #[test]
    fn default_excludes_without_dockerignore() {
        let tmp = tempfile::tempdir().unwrap();
        let d = tmp.path();
        fs::create_dir_all(d.join(".git")).unwrap();
        fs::create_dir_all(d.join("app/__pycache__")).unwrap();
        fs::create_dir_all(d.join("node_modules/x")).unwrap();
        fs::write(d.join(".git/HEAD"), "x").unwrap();
        fs::write(d.join("app/__pycache__/a.pyc"), "x").unwrap();
        fs::write(d.join("app/main.py"), "x").unwrap();
        fs::write(d.join("app/old.pyc"), "x").unwrap();
        fs::write(d.join("node_modules/x/i.js"), "x").unwrap();
        fs::write(d.join("Dockerfile"), "x").unwrap();
        fs::write(d.join(".env"), "x").unwrap();
        assert_eq!(rels(d), ["Dockerfile", "app/main.py"]);
    }

    #[test]
    fn dockerignore_negation_reincludes_under_excluded_dir() {
        let tmp = tempfile::tempdir().unwrap();
        let d = tmp.path();
        fs::create_dir_all(d.join("data/sub")).unwrap();
        fs::write(d.join("data/a.txt"), "x").unwrap();
        fs::write(d.join("data/sub/keep.txt"), "x").unwrap();
        fs::write(d.join("main.rs"), "x").unwrap();
        fs::write(
            d.join(".dockerignore"),
            "# comment\n\ndata/\n!data/sub/keep.txt\n",
        )
        .unwrap();
        // os.walk と同じく、ディレクトリ直下のファイルが配下より先
        assert_eq!(rels(d), [".dockerignore", "main.rs", "data/sub/keep.txt"]);
    }

    #[test]
    fn dockerignore_replaces_default_excludes() {
        let tmp = tempfile::tempdir().unwrap();
        let d = tmp.path();
        fs::create_dir_all(d.join("node_modules")).unwrap();
        fs::write(d.join("node_modules/i.js"), "x").unwrap();
        fs::write(d.join(".dockerignore"), "*.log\n").unwrap();
        fs::write(d.join("x.log"), "x").unwrap();
        assert_eq!(rels(d), [".dockerignore", "node_modules/i.js"]);
    }

    #[test]
    fn symlinks_follow_os_walk() {
        let tmp = tempfile::tempdir().unwrap();
        let d = tmp.path();
        fs::create_dir_all(d.join("real")).unwrap();
        fs::write(d.join("real/f.txt"), "x").unwrap();
        symlink(d.join("real"), d.join("linkdir")).unwrap();
        symlink(d.join("real/f.txt"), d.join("linkfile")).unwrap();
        symlink(d.join("missing"), d.join("broken")).unwrap();
        let spec = load_dockerignore(d).unwrap();
        let files = iter_source_files(d, &spec).unwrap();
        let listed: Vec<(&str, bool)> = files
            .iter()
            .map(|f| (f.rel.as_str(), f.is_broken_symlink()))
            .collect();
        assert_eq!(
            listed,
            [("broken", true), ("linkfile", false), ("real/f.txt", false)]
        );
    }

    #[test]
    fn world_read_warns_general_and_errors_on_pocket_toml() {
        let tmp = tempfile::tempdir().unwrap();
        let d = tmp.path();
        fs::write(d.join("ok.txt"), "x").unwrap();
        fs::write(d.join("secret.txt"), "x").unwrap();
        fs::set_permissions(d.join("secret.txt"), fs::Permissions::from_mode(0o600)).unwrap();
        assert_eq!(
            find_files_without_world_read(d).unwrap(),
            [("secret.txt".to_string(), 0o600)]
        );
        warn_files_without_world_read(d).unwrap();

        fs::write(d.join("pocket.toml"), "x").unwrap();
        fs::set_permissions(d.join("pocket.toml"), fs::Permissions::from_mode(0o640)).unwrap();
        let err = warn_files_without_world_read(d).unwrap_err().to_string();
        assert!(err.starts_with("build context の pocket.toml に other-read がありません"));
    }
}
