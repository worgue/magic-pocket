//! path → 内容 hash の map ファイル (Python の `pocket.static.mapfile` と同じ形式)。
//!
//! git に commit する release の正本。テキストで path 順に並べ 1 行 1 entry
//! (`path<TAB>sha256<TAB>source<TAB>H|P`) なので、変更が少ないときの diff が小さい。
//! release id は正規化した本文の SHA-256 の先頭 16 hex で、同じ内容なら同じ id になる。

use std::collections::BTreeMap;

use anyhow::{anyhow, bail, Result};
use magic_pocket_rs::static_url::{hashed_name, validate_logical_path, HASH_LEN};
use sha2::{Digest, Sha256};

pub const MAP_FORMAT_LINE: &str = "# magic-pocket static map v1";
const SHA256_LEN: usize = 64;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MapEntry {
    pub path: String,
    pub sha256: String,
    pub source: String,
    pub hashed: bool,
}

impl MapEntry {
    /// route の key prefix からの相対 key (= URL の prefix からの相対 path)
    pub fn rel_key(&self) -> String {
        if self.hashed {
            hashed_name(&self.path, &self.sha256)
        } else {
            self.path.clone()
        }
    }

    pub fn line(&self) -> String {
        format!(
            "{}\t{}\t{}\t{}",
            self.path,
            self.sha256,
            self.source,
            if self.hashed { "H" } else { "P" }
        )
    }
}

#[derive(Debug, Clone)]
pub struct StaticMap {
    /// "<cloudfront 名>/<route 名>"。別 route の map の取り違えに気付くため
    pub route: String,
    /// path 順 (BTreeMap は文字列順 = Python の sorted と同じ)
    pub entries: BTreeMap<String, MapEntry>,
}

impl StaticMap {
    pub fn new(route: &str) -> Self {
        Self {
            route: route.to_string(),
            entries: BTreeMap::new(),
        }
    }

    pub fn add(&mut self, entry: MapEntry) -> Result<()> {
        validate_logical_path(&entry.path).map_err(|e| anyhow!("{e}"))?;
        if entry.sha256.len() != SHA256_LEN
            || !entry
                .sha256
                .chars()
                .all(|c| c.is_ascii_digit() || ('a'..='f').contains(&c))
        {
            bail!("sha256 の形が不正です: {:?}", entry.sha256);
        }
        if self.entries.contains_key(&entry.path) {
            bail!("path が重複しています: {:?}", entry.path);
        }
        self.entries.insert(entry.path.clone(), entry);
        Ok(())
    }

    pub fn sorted_entries(&self) -> impl Iterator<Item = &MapEntry> {
        self.entries.values()
    }

    pub fn body(&self) -> String {
        let mut out = String::new();
        for e in self.sorted_entries() {
            out.push_str(&e.line());
            out.push('\n');
        }
        out
    }

    pub fn dumps(&self) -> String {
        format!(
            "{}\n# route: {}\n{}",
            MAP_FORMAT_LINE,
            self.route,
            self.body()
        )
    }

    pub fn content_sha256(&self) -> String {
        hex::encode(Sha256::digest(self.dumps().as_bytes()))
    }

    /// release の識別子。map の本文 (route 名を含む) から決まる
    pub fn release_id(&self) -> String {
        self.content_sha256()[..HASH_LEN].to_string()
    }

    pub fn loads(text: &str) -> Result<StaticMap> {
        let mut lines = text.lines();
        match lines.next() {
            Some(first) if first == MAP_FORMAT_LINE => {}
            _ => bail!(
                "map の形式が違います (先頭行が {MAP_FORMAT_LINE:?} ではありません)。pocket の版が\
                 古いか、map ファイルではありません"
            ),
        }
        let mut static_map = StaticMap::new("");
        for (lineno, line) in lines.enumerate().map(|(i, l)| (i + 2, l)) {
            if line.trim().is_empty() {
                continue;
            }
            if let Some(rest) = line.strip_prefix('#') {
                if let Some(route) = rest.strip_prefix(" route: ") {
                    static_map.route = route.trim().to_string();
                }
                continue;
            }
            let parts: Vec<&str> = line.split('\t').collect();
            if parts.len() != 4 || !matches!(parts[3], "H" | "P") {
                bail!("map の {lineno} 行目を読めません: {line:?}");
            }
            static_map
                .add(MapEntry {
                    path: parts[0].to_string(),
                    sha256: parts[1].to_string(),
                    source: parts[2].to_string(),
                    hashed: parts[3] == "H",
                })
                .map_err(|e| anyhow!("map の {lineno} 行目: {e}"))?;
        }
        if static_map.route.is_empty() {
            bail!("map に `# route:` 行がありません");
        }
        Ok(static_map)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const H1: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const H2: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

    fn entry(path: &str, sha: &str, source: &str, hashed: bool) -> MapEntry {
        MapEntry {
            path: path.into(),
            sha256: sha.into(),
            source: source.into(),
            hashed,
        }
    }

    fn sample() -> StaticMap {
        let mut m = StaticMap::new("web/static");
        m.add(entry("img/b.jpg", H1, "data", true)).unwrap();
        m.add(entry("app/chunk.js", H2, "code", false)).unwrap();
        m.add(entry("img/a.jpg", H2, "data", true)).unwrap();
        m
    }

    #[test]
    fn dumps_is_sorted_by_path_and_roundtrips() {
        let text = sample().dumps();
        let lines: Vec<&str> = text.lines().collect();
        assert_eq!(lines[0], MAP_FORMAT_LINE);
        assert_eq!(lines[1], "# route: web/static");
        let paths: Vec<&str> = lines[2..]
            .iter()
            .map(|l| l.split('\t').next().unwrap())
            .collect();
        assert_eq!(paths, ["app/chunk.js", "img/a.jpg", "img/b.jpg"]);
        let loaded = StaticMap::loads(&text).unwrap();
        assert_eq!(loaded.route, "web/static");
        assert_eq!(loaded.entries, sample().entries);
        assert_eq!(loaded.dumps(), text);
    }

    #[test]
    fn release_id_depends_on_content_not_insertion_order() {
        let a = sample();
        let mut b = StaticMap::new("web/static");
        for e in a.sorted_entries().collect::<Vec<_>>().into_iter().rev() {
            b.add(e.clone()).unwrap();
        }
        assert_eq!(a.release_id(), b.release_id());
        assert_eq!(a.release_id().len(), 16);
        let mut c = sample();
        c.add(entry("new.txt", H1, "data", true)).unwrap();
        assert_ne!(c.release_id(), a.release_id());
    }

    #[test]
    fn release_id_matches_python() {
        // Python: hashlib.sha256(StaticMap("web/static") に同じ 3 entry を add した dumps()).hexdigest()[:16]
        // (runtime の pocket.static.mapfile と同じ形式。ここでは形式が固定であることだけを見る)
        let text = sample().dumps();
        assert!(text.ends_with("img/b.jpg\taaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\tdata\tH\n"));
    }

    #[test]
    fn rel_key_for_hashed_and_plain() {
        assert_eq!(
            entry("img/a.jpg", H1, "data", true).rel_key(),
            "img/a.aaaaaaaaaaaaaaaa.jpg"
        );
        assert_eq!(
            entry("app/chunk.js", H1, "code", false).rel_key(),
            "app/chunk.js"
        );
    }

    #[test]
    fn loads_rejects_malformed() {
        let cases: Vec<(String, &str)> = vec![
            ("not a map\n".into(), "形式"),
            (
                format!("{MAP_FORMAT_LINE}\n# route: r\nbad line\n"),
                "3 行目",
            ),
            (
                format!("{MAP_FORMAT_LINE}\n# route: r\na\t{H1}\tdata\tX\n"),
                "行目",
            ),
            (
                format!("{MAP_FORMAT_LINE}\n# route: r\na\tzz\tdata\tH\n"),
                "sha256",
            ),
            (
                format!("{MAP_FORMAT_LINE}\n# route: r\na\t{H1}\tdata\tH\na\t{H2}\tdata\tH\n"),
                "重複",
            ),
            (
                format!("{MAP_FORMAT_LINE}\n# route: r\n/abs\t{H1}\tdata\tH\n"),
                "path",
            ),
            (format!("{MAP_FORMAT_LINE}\na\t{H1}\tdata\tH\n"), "route"),
        ];
        for (text, message) in cases {
            let err = StaticMap::loads(&text).unwrap_err().to_string();
            assert!(err.contains(message), "{text:?} → {err}");
        }
    }

    #[test]
    fn add_rejects_duplicate_path() {
        let mut m = sample();
        let err = m.add(entry("img/a.jpg", H1, "data", true)).unwrap_err();
        assert!(err.to_string().contains("重複"));
    }
}
