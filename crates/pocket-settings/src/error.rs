//! 設定の読み込みエラー。
//!
//! Python (pydantic) のエラーと比べられるよう、どの field か (`path`) と種類
//! (`kind`) を持つ。比べ方は test vector の規約による: `Value` (独自 validator と
//! 前処理) は `message` まで、`Missing` / `Extra` / `Type` は `path` と `kind` だけ、
//! `Syntax` は `kind` だけ。

use std::fmt;

use serde::Serialize;

/// path の 1 要素 (field 名・dict の key、または配列の index)
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(untagged)]
pub enum PathSeg {
    Key(String),
    Index(usize),
}

impl fmt::Display for PathSeg {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            PathSeg::Key(k) => write!(f, "{k}"),
            PathSeg::Index(i) => write!(f, "{i}"),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum ErrorKind {
    /// toml として読めない
    Syntax,
    /// 必須の field が無い
    Missing,
    /// 定義されていない field がある
    Extra,
    /// 型・形 (正規表現・範囲を含む) が合わない
    Type,
    /// 独自の検証 (前処理と validator) に通らない
    Value,
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub struct SettingsError {
    pub path: Vec<PathSeg>,
    pub kind: ErrorKind,
    pub message: String,
}

impl SettingsError {
    pub fn value(path: Vec<PathSeg>, message: impl Into<String>) -> Self {
        Self {
            path,
            kind: ErrorKind::Value,
            message: message.into(),
        }
    }

    pub(crate) fn syntax(message: impl Into<String>) -> Self {
        Self {
            path: Vec::new(),
            kind: ErrorKind::Syntax,
            message: message.into(),
        }
    }
}

impl fmt::Display for SettingsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if self.path.is_empty() {
            write!(f, "{}", self.message)
        } else {
            let path: Vec<String> = self.path.iter().map(|p| p.to_string()).collect();
            write!(f, "{}: {}", path.join("."), self.message)
        }
    }
}

/// path を組み立てる (`path(&["general", "region"])`)
pub fn path(keys: &[&str]) -> Vec<PathSeg> {
    keys.iter()
        .map(|k| PathSeg::Key((*k).to_string()))
        .collect()
}

/// `base` の後ろに key を足した path
pub fn child(base: &[PathSeg], key: &str) -> Vec<PathSeg> {
    let mut p = base.to_vec();
    p.push(PathSeg::Key(key.to_string()));
    p
}

/// serde のエラーを path / kind 付きに直す。
///
/// serde のエラーは文だけを持つので、種類は文の先頭で見分ける
/// (`missing field` / `unknown field`、それ以外は型の不一致)。missing は
/// pydantic と同じく欠けた field 名まで path に含める。
pub(crate) fn from_serde(err: serde_path_to_error::Error<toml::de::Error>) -> SettingsError {
    let mut path = Vec::new();
    for seg in err.path().iter() {
        match seg {
            serde_path_to_error::Segment::Map { key } => path.push(PathSeg::Key(key.clone())),
            serde_path_to_error::Segment::Seq { index } => path.push(PathSeg::Index(*index)),
            _ => {}
        }
    }
    let message = err.inner().message().to_string();
    let kind = if let Some(name) = backquoted(&message, "missing field `") {
        path.push(PathSeg::Key(name));
        ErrorKind::Missing
    } else if let Some(name) = backquoted(&message, "unknown field `") {
        if path.last() != Some(&PathSeg::Key(name.clone())) {
            path.push(PathSeg::Key(name));
        }
        ErrorKind::Extra
    } else {
        ErrorKind::Type
    };
    SettingsError {
        path,
        kind,
        message,
    }
}

fn backquoted(message: &str, prefix: &str) -> Option<String> {
    let rest = message.strip_prefix(prefix)?;
    let end = rest.find('`')?;
    Some(rest[..end].to_string())
}
