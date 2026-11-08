//! route の `sources` からローカルファイルを列挙する (旧 Python 版の
//! `StaticPublisher.enumerate_sources` / `_walk_files` と同じ規則)。
//!
//! - `dir` 配下を再帰する。symlink は追わず、`.pocket` ディレクトリは除く
//! - include / exclude は gitignore 風 (Python は pathspec、こちらは `ignore` crate)。
//!   include があれば合うものだけ、次に exclude に合うものを除く
//! - 論理 path = `prefix` + 相対 path。複数 source で同じ論理 path はエラー

use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{anyhow, bail, Context, Result};
use ignore::gitignore::{Gitignore, GitignoreBuilder};
use magic_pocket_rs::config::StaticSourceConfig;
use magic_pocket_rs::static_url::validate_logical_path;

use crate::mapfile::MapEntry;

#[derive(Debug, Clone)]
pub struct LocalFile {
    /// 論理 path (= map の path)
    pub path: String,
    pub file: PathBuf,
    pub source: String,
    pub hashed: bool,
    pub size: u64,
    pub sha256: String,
    pub md5: String,
}

impl LocalFile {
    pub fn entry(&self) -> MapEntry {
        MapEntry {
            path: self.path.clone(),
            sha256: self.sha256.clone(),
            source: self.source.clone(),
            hashed: self.hashed,
        }
    }
}

/// Python の `settings.StaticSource.check_fields` と同じ検査 (設定の読み込み時ではなく
/// publish 側で行う。runtime の読み込みを厳しくしないため)
pub fn validate_source(source: &StaticSourceConfig) -> Result<()> {
    if source.name.is_empty()
        || !source
            .name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
    {
        bail!(
            "sources の name は英数字と - _ だけで書いてください: {:?}",
            source.name
        );
    }
    if source.dir.is_empty() || source.dir.starts_with('/') {
        bail!(
            "sources の dir は pocket.toml からの相対パスで書いてください: {:?}",
            source.dir
        );
    }
    if source.prefix.starts_with('/') {
        bail!(
            "sources の prefix は先頭の / を付けずに書いてください: {:?}",
            source.prefix
        );
    }
    if !source.prefix.is_empty() && !source.prefix.ends_with('/') {
        bail!(
            "sources の prefix は末尾を / にしてください (例: 'img/'): {:?}",
            source.prefix
        );
    }
    Ok(())
}

fn build_matcher(root: &Path, patterns: &[String]) -> Result<Option<Gitignore>> {
    if patterns.is_empty() {
        return Ok(None);
    }
    let mut builder = GitignoreBuilder::new(root);
    for pattern in patterns {
        builder
            .add_line(None, pattern)
            .with_context(|| format!("pattern {pattern:?} を読めません"))?;
    }
    Ok(Some(builder.build()?))
}

/// pathspec の `match_file` と同じ判定: 最後に合った pattern が否定でなければ true
fn matches(matcher: &Gitignore, rel: &str) -> bool {
    matcher
        .matched_path_or_any_parents(Path::new(rel), false)
        .is_ignore()
}

pub fn enumerate_sources(
    base_dir: &Path,
    sources: &[StaticSourceConfig],
) -> Result<Vec<LocalFile>> {
    let mut files: Vec<LocalFile> = Vec::new();
    let mut seen: std::collections::HashMap<String, String> = std::collections::HashMap::new();
    for source in sources {
        validate_source(source)?;
        let src_dir = base_dir.join(&source.dir);
        if !src_dir.is_dir() {
            bail!(
                "source '{}' の dir が見つかりません: {}",
                source.name,
                src_dir.display()
            );
        }
        let include = build_matcher(&src_dir, &source.include)?;
        let exclude = build_matcher(&src_dir, &source.exclude)?;
        for rel in walk_files(&src_dir)? {
            if let Some(inc) = &include {
                if !matches(inc, &rel) {
                    continue;
                }
            }
            if let Some(exc) = &exclude {
                if matches(exc, &rel) {
                    continue;
                }
            }
            let logical = format!("{}{}", source.prefix, rel);
            validate_logical_path(&logical)
                .map_err(|e| anyhow!("source '{}' のファイル名を使えません: {e}", source.name))?;
            if let Some(other) = seen.get(&logical) {
                bail!(
                    "path {:?} が source '{}' と '{}' の両方にあります",
                    logical,
                    other,
                    source.name
                );
            }
            seen.insert(logical.clone(), source.name.clone());
            let file = src_dir.join(&rel);
            let size = fs::metadata(&file)
                .with_context(|| format!("{} を読めません", file.display()))?
                .len();
            files.push(LocalFile {
                path: logical,
                file,
                source: source.name.clone(),
                hashed: source.hashed,
                size,
                sha256: String::new(),
                md5: String::new(),
            });
        }
    }
    files.sort_by(|a, b| a.path.cmp(&b.path));
    Ok(files)
}

/// root 配下のファイルを相対 posix path で返す。symlink は追わず .pocket は除く
fn walk_files(root: &Path) -> Result<Vec<String>> {
    let mut out = Vec::new();
    walk_dir(root, root, &mut out)?;
    Ok(out)
}

fn walk_dir(root: &Path, dir: &Path, out: &mut Vec<String>) -> Result<()> {
    let mut entries: Vec<fs::DirEntry> = fs::read_dir(dir)
        .with_context(|| format!("{} を読めません", dir.display()))?
        .collect::<std::io::Result<Vec<_>>>()?;
    entries.sort_by_key(|e| e.file_name());
    let mut subdirs = Vec::new();
    for entry in entries {
        let path = entry.path();
        let meta = fs::symlink_metadata(&path)?;
        if meta.file_type().is_symlink() {
            continue;
        }
        if meta.is_dir() {
            if entry.file_name() != ".pocket" {
                subdirs.push(path);
            }
        } else if meta.is_file() {
            let rel = path
                .strip_prefix(root)
                .expect("root 配下")
                .components()
                .map(|c| c.as_os_str().to_string_lossy().into_owned())
                .collect::<Vec<_>>()
                .join("/");
            out.push(rel);
        }
    }
    for sub in subdirs {
        walk_dir(root, &sub, out)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn source(name: &str, dir: &str) -> StaticSourceConfig {
        StaticSourceConfig {
            name: name.into(),
            dir: dir.into(),
            prefix: String::new(),
            include: vec![],
            exclude: vec![],
            hashed: true,
        }
    }

    #[test]
    fn walk_skips_symlink_and_pocket_dir_and_sorts() {
        let tmp = tempdir();
        fs::create_dir_all(tmp.join("data/img")).unwrap();
        fs::create_dir_all(tmp.join("data/.pocket")).unwrap();
        fs::write(tmp.join("data/img/a.jpg"), b"A").unwrap();
        fs::write(tmp.join("data/b.txt"), b"B").unwrap();
        fs::write(tmp.join("data/.pocket/x"), b"X").unwrap();
        #[cfg(unix)]
        std::os::unix::fs::symlink(tmp.join("data/b.txt"), tmp.join("data/link.txt")).unwrap();
        let files = enumerate_sources(&tmp, &[source("data", "data")]).unwrap();
        let paths: Vec<&str> = files.iter().map(|f| f.path.as_str()).collect();
        assert_eq!(paths, ["b.txt", "img/a.jpg"]);
        fs::remove_dir_all(&tmp).unwrap();
    }

    #[test]
    fn include_exclude_gitignore_semantics() {
        let tmp = tempdir();
        fs::create_dir_all(tmp.join("d/sub/deep")).unwrap();
        for p in [
            "d/a.tmp",
            "d/a.txt",
            "d/sub/b.tmp",
            "d/sub/keep.tmp",
            "d/sub/deep/c.txt",
            "d/root.txt",
        ] {
            fs::write(tmp.join(p), b"x").unwrap();
        }
        let mut s = source("d", "d");
        s.exclude = vec![
            "*.tmp".into(),
            "!keep.tmp".into(),
            "/root.txt".into(),
            "deep/".into(),
        ];
        let files = enumerate_sources(&tmp, &[s]).unwrap();
        let paths: Vec<&str> = files.iter().map(|f| f.path.as_str()).collect();
        assert_eq!(paths, ["a.txt", "sub/keep.tmp"]);
        let mut s = source("d", "d");
        s.include = vec!["sub/**".into()];
        s.exclude = vec!["*.tmp".into()];
        let files = enumerate_sources(&tmp, &[s]).unwrap();
        let paths: Vec<&str> = files.iter().map(|f| f.path.as_str()).collect();
        assert_eq!(paths, ["sub/deep/c.txt"]);
        fs::remove_dir_all(&tmp).unwrap();
    }

    #[test]
    fn logical_path_collision_between_sources_is_rejected() {
        let tmp = tempdir();
        fs::create_dir_all(tmp.join("x")).unwrap();
        fs::create_dir_all(tmp.join("y")).unwrap();
        fs::write(tmp.join("x/a.txt"), b"A").unwrap();
        fs::write(tmp.join("y/a.txt"), b"B").unwrap();
        let err = enumerate_sources(&tmp, &[source("x", "x"), source("y", "y")]).unwrap_err();
        assert!(err.to_string().contains("両方にあります"), "{err}");
        fs::remove_dir_all(&tmp).unwrap();
    }

    #[test]
    fn validate_source_rules() {
        let mut s = source("bad name", "d");
        assert!(validate_source(&s).is_err());
        s = source("ok", "/abs");
        assert!(validate_source(&s).is_err());
        s = source("ok", "d");
        s.prefix = "img".into();
        assert!(validate_source(&s).is_err());
        s.prefix = "/img/".into();
        assert!(validate_source(&s).is_err());
        s.prefix = "img/".into();
        validate_source(&s).unwrap();
    }

    fn tempdir() -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "pocket-cli-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&dir).unwrap();
        dir
    }
}
