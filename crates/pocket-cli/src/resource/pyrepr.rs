//! Python の `repr` / `pprint.pprint` と同じ文字列 (JSON の値に限る)。
//!
//! `pocket resource s3 context` (`pprint(model_dump())`) と `container secrets list`
//! (managed secret の options を `%s` で出す) の出力を Python 版と一致させるため。
//! 扱うのは dict / list / str / int / float / bool / None だけ

use serde_json::Value as Json;

/// `pprint.pprint` の既定の幅
const WIDTH: usize = 80;

/// Python の `repr(value)`。`sort_dicts` は pprint と同じく dict の key を並べ替える
pub fn repr(value: &Json, sort_dicts: bool) -> String {
    match value {
        Json::Null => "None".to_string(),
        Json::Bool(true) => "True".to_string(),
        Json::Bool(false) => "False".to_string(),
        Json::Number(_) => magic_pocket_settings::pyjson::dumps(value),
        Json::String(s) => repr_str(s),
        Json::Array(items) => {
            let parts: Vec<String> = items.iter().map(|v| repr(v, sort_dicts)).collect();
            format!("[{}]", parts.join(", "))
        }
        Json::Object(map) => {
            let parts: Vec<String> = entries(map, sort_dicts)
                .into_iter()
                .map(|(k, v)| format!("{}: {}", repr_str(k), repr(v, sort_dicts)))
                .collect();
            format!("{{{}}}", parts.join(", "))
        }
    }
}

fn entries(map: &serde_json::Map<String, Json>, sort: bool) -> Vec<(&String, &Json)> {
    let mut items: Vec<(&String, &Json)> = map.iter().collect();
    if sort {
        items.sort_by(|a, b| a.0.cmp(b.0));
    }
    items
}

/// Python の `repr(str)`: `'` を含み `"` を含まなければ `"` で囲む
pub fn repr_str(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::new();
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// `pprint.pprint(value)` の出力 (末尾の改行なし)
pub fn pformat(value: &Json) -> String {
    let mut out = String::new();
    format(value, &mut out, 0, 0, 0);
    out
}

fn len(s: &str) -> usize {
    s.chars().count()
}

/// `PrettyPrinter._format`
fn format(value: &Json, out: &mut String, indent: usize, allowance: usize, level: usize) {
    let rep = repr(value, true);
    let max_width = WIDTH.saturating_sub(indent + allowance);
    if len(&rep) > max_width {
        match value {
            Json::Object(map) if !map.is_empty() => {
                return pprint_dict(map, out, indent, allowance, level + 1)
            }
            Json::Array(items) if !items.is_empty() => {
                return pprint_list(items, out, indent, allowance, level + 1)
            }
            Json::String(s) => return pprint_str(s, out, indent, allowance, level + 1),
            _ => {}
        }
    }
    out.push_str(&rep);
}

fn pprint_dict(
    map: &serde_json::Map<String, Json>,
    out: &mut String,
    indent: usize,
    allowance: usize,
    level: usize,
) {
    out.push('{');
    let items = entries(map, true);
    let indent = indent + 1;
    let allowance = allowance + 1;
    let last_index = items.len() - 1;
    for (i, (key, value)) in items.into_iter().enumerate() {
        let last = i == last_index;
        let rep = repr_str(key);
        out.push_str(&rep);
        out.push_str(": ");
        format(
            value,
            out,
            indent + len(&rep) + 2,
            if last { allowance } else { 1 },
            level,
        );
        if !last {
            out.push_str(",\n");
            out.push_str(&" ".repeat(indent));
        }
    }
    out.push('}');
}

fn pprint_list(items: &[Json], out: &mut String, indent: usize, allowance: usize, level: usize) {
    out.push('[');
    let indent = indent + 1;
    let allowance = allowance + 1;
    let last_index = items.len() - 1;
    for (i, item) in items.iter().enumerate() {
        let last = i == last_index;
        if i > 0 {
            out.push_str(",\n");
            out.push_str(&" ".repeat(indent));
        }
        format(item, out, indent, if last { allowance } else { 1 }, level);
    }
    out.push(']');
}

/// `str.splitlines(keepends=True)` (改行は `\n` / `\r\n` / `\r` だけを見る)
fn split_lines_keep(s: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let bytes = s.as_bytes();
    let mut start = 0;
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'\n' => {
                lines.push(&s[start..=i]);
                start = i + 1;
            }
            b'\r' => {
                let end = if bytes.get(i + 1) == Some(&b'\n') {
                    i + 1
                } else {
                    i
                };
                lines.push(&s[start..=end]);
                i = end;
                start = end + 1;
            }
            _ => {}
        }
        i += 1;
    }
    if start < s.len() {
        lines.push(&s[start..]);
    }
    lines
}

/// `re.findall(r'\S*\s*', line)` の空でない要素
fn words(line: &str) -> Vec<&str> {
    let mut parts = Vec::new();
    let mut start = 0;
    let mut in_space = false;
    for (i, c) in line.char_indices() {
        if c.is_whitespace() {
            in_space = true;
        } else if in_space {
            parts.push(&line[start..i]);
            start = i;
            in_space = false;
        }
    }
    if start < line.len() {
        parts.push(&line[start..]);
    }
    parts
}

/// `PrettyPrinter._pprint_str`: 長い文字列を語の切れ目で分け、`(` `)` で括って並べる
fn pprint_str(s: &str, out: &mut String, indent: usize, allowance: usize, level: usize) {
    let (indent, allowance) = if level == 1 {
        (indent + 1, allowance + 1)
    } else {
        (indent, allowance)
    };
    let max_width = WIDTH.saturating_sub(indent);
    let mut max_width1 = max_width;
    let lines = split_lines_keep(s);
    let mut chunks: Vec<String> = Vec::new();
    let mut rep = String::new();
    for (i, line) in lines.iter().enumerate() {
        rep = repr_str(line);
        let last_line = i == lines.len() - 1;
        if last_line {
            max_width1 = max_width1.saturating_sub(allowance);
        }
        if len(&rep) <= max_width1 {
            chunks.push(rep.clone());
            continue;
        }
        let parts = words(line);
        let mut max_width2 = max_width;
        let mut current = String::new();
        for (j, part) in parts.iter().enumerate() {
            let candidate = format!("{current}{part}");
            if j == parts.len() - 1 && last_line {
                max_width2 = max_width2.saturating_sub(allowance);
            }
            if len(&repr_str(&candidate)) > max_width2 {
                if !current.is_empty() {
                    chunks.push(repr_str(&current));
                }
                current = part.to_string();
            } else {
                current = candidate;
            }
        }
        if !current.is_empty() {
            chunks.push(repr_str(&current));
        }
    }
    if chunks.len() == 1 {
        out.push_str(&rep);
        return;
    }
    if level == 1 {
        out.push('(');
    }
    for (i, chunk) in chunks.iter().enumerate() {
        if i > 0 {
            out.push('\n');
            out.push_str(&" ".repeat(indent));
        }
        out.push_str(chunk);
    }
    if level == 1 {
        out.push(')');
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn test_repr_scalars_and_quotes() {
        assert_eq!(repr(&json!(null), false), "None");
        assert_eq!(repr(&json!(true), false), "True");
        assert_eq!(repr(&json!(3), false), "3");
        assert_eq!(repr(&json!(1.5), false), "1.5");
        assert_eq!(repr_str("a'b"), "\"a'b\"");
        assert_eq!(repr_str("a'b\""), "'a\\'b\"'");
        assert_eq!(repr_str("改行\n"), "'改行\\n'");
    }

    #[test]
    fn test_repr_keeps_insertion_order_unless_sorted() {
        let value = json!({"length": 50, "a": "x"});
        assert_eq!(repr(&value, false), "{'length': 50, 'a': 'x'}");
        assert_eq!(repr(&value, true), "{'a': 'x', 'length': 50}");
    }

    #[test]
    fn test_pformat_short_fits_on_one_line() {
        let value = json!({"region": "ap-northeast-1", "versioning": false, "cors": null});
        assert_eq!(
            pformat(&value),
            "{'cors': None, 'region': 'ap-northeast-1', 'versioning': False}"
        );
    }

    /// 期待値は Python 3.12 の `pprint.pprint` の出力
    #[test]
    fn test_pformat_wraps_like_python() {
        let value = json!({
            "bucket_name": "sandbox-example-project-pocket-bucket",
            "cors": {"cloudfront_names": ["main"], "methods": ["GET", "HEAD"]},
            "lifecycle_rules": [
                {"expiration_days": 30, "id": "tmp", "noncurrent_version_expiration_days": null, "prefix": "tmp/"}
            ],
            "region": "ap-northeast-1",
            "versioning": true
        });
        assert_eq!(
            pformat(&value),
            "{'bucket_name': 'sandbox-example-project-pocket-bucket',\n \
             'cors': {'cloudfront_names': ['main'], 'methods': ['GET', 'HEAD']},\n \
             'lifecycle_rules': [{'expiration_days': 30,\n                      \
             'id': 'tmp',\n                      \
             'noncurrent_version_expiration_days': None,\n                      \
             'prefix': 'tmp/'}],\n \
             'region': 'ap-northeast-1',\n \
             'versioning': True}"
        );
    }

    /// 期待値は Python 3.12 の `pprint.pformat` の出力
    #[test]
    fn test_pformat_splits_long_string() {
        let words = |n: usize, w: &str| vec![w; n].join(" ");
        assert_eq!(
            pformat(&json!({"k": words(30, "word"), "z": [words(25, "long")]})),
            "{'k': 'word word word word word word word word word word word word word word '\n      \
             'word word word word word word word word word word word word word word '\n      \
             'word word',\n \
             'z': ['long long long long long long long long long long long long long long '\n       \
             'long long long long long long long long long long long']}"
        );
        // 一番外の文字列は ( ) で括る
        assert_eq!(
            pformat(&json!(words(30, "word"))),
            "('word word word word word word word word word word word word word word word '\n \
             'word word word word word word word word word word word word word word word')"
        );
    }
}
