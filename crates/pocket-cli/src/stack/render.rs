//! Jinja2 の template の描画 (Python 版 `render_template` と同じ結果)。
//!
//! template は Python 版 CLI のパッケージにあるものをそのまま埋め込む (移行中は
//! 1 か所で保守する)。Jinja2 (Python) と minijinja で出力が変わる箇所は、ここで
//! Python 側に合わせる:
//!
//! - 値の出力: `True` / `False` / `None` (minijinja は `true` / `false` / `none`)
//! - `tojson`: `json.dumps(sort_keys=True)` の文字列 (区切り `", "` / `": "`、
//!   `ensure_ascii`) に、Jinja2 の HTML 安全化 (`<` `>` `&` `'` を `\uXXXX`) を掛ける
//! - auto escape: Jinja2 の `select_autoescape()` は `.yaml` を対象にしない
//! - 描画後に、行の中身が `#` / `# prettier-ignore` だけの行を除く (template の
//!   制御構文を yaml のコメントに置いているため)

use anyhow::{anyhow, Context as _, Result};
use magic_pocket_settings::pyjson;
use minijinja::value::{Kwargs, Serde, ValueKind};
use minijinja::{AutoEscape, Environment, Error, ErrorKind, Output, State, Value};
use serde_json::Value as Json;

/// template の名前 (Python 版の `Stack.template_filename`)
pub const TEMPLATES: &[(&str, &str)] = &[
    (
        "cloudfront",
        include_str!(
            "../../../../packages/magic-pocket-cli/pocket_cli/templates/cloudformation/cloudfront.yaml"
        ),
    ),
    (
        "cloudfront_acm",
        include_str!(
            "../../../../packages/magic-pocket-cli/pocket_cli/templates/cloudformation/cloudfront_acm.yaml"
        ),
    ),
    (
        "cloudfront_keys",
        include_str!(
            "../../../../packages/magic-pocket-cli/pocket_cli/templates/cloudformation/cloudfront_keys.yaml"
        ),
    ),
    (
        "cloudfront_waf",
        include_str!(
            "../../../../packages/magic-pocket-cli/pocket_cli/templates/cloudformation/cloudfront_waf.yaml"
        ),
    ),
    (
        "container",
        include_str!(
            "../../../../packages/magic-pocket-cli/pocket_cli/templates/cloudformation/container.yaml"
        ),
    ),
    (
        "vpc",
        include_str!(
            "../../../../packages/magic-pocket-cli/pocket_cli/templates/cloudformation/vpc.yaml"
        ),
    ),
];

/// `cloudformation/<名前>.yaml` を `variables` で描画する
pub fn render_template(name: &str, variables: &Json) -> Result<String> {
    let env = environment()?;
    let template = env
        .get_template(name)
        .map_err(|e| anyhow!("template {name} がありません: {e}"))?;
    let rendered = template
        .render(Value::from(Serde(variables)))
        .map_err(|e| anyhow!("{e:#}"))
        .with_context(|| format!("template {name} の描画に失敗しました"))?;
    Ok(drop_control_lines(&rendered))
}

fn environment() -> Result<Environment<'static>> {
    let mut env = Environment::new();
    env.set_auto_escape_callback(|_| AutoEscape::None);
    env.set_formatter(python_formatter);
    env.set_unknown_method_callback(minijinja_contrib::pycompat::unknown_method_callback);
    env.add_filter("tojson", tojson);
    env.add_filter("indent", indent);
    for (name, source) in TEMPLATES {
        env.add_template(name, source)
            .map_err(|e| anyhow!("template {name} を読めません: {e:#}"))?;
    }
    Ok(env)
}

/// Python の `str(value)` と同じ文字列で出す (Jinja2 の `{{ value }}`)
fn python_formatter(out: &mut Output, _state: &mut State, value: &Value) -> Result<(), Error> {
    let written = match value.kind() {
        ValueKind::Undefined => Ok(()),
        ValueKind::None => out.write_str("None"),
        ValueKind::Bool => out.write_str(if value.is_true() { "True" } else { "False" }),
        ValueKind::Seq | ValueKind::Map => {
            // Python の repr (`['a']` / `{'a': 1}`) は再現しない。template は値を
            // tojson 経由で出すので、ここに来たら template 側の誤り
            return Err(Error::new(
                ErrorKind::InvalidOperation,
                "list / dict を直接出力しています (tojson を使ってください)",
            ));
        }
        _ => write!(out, "{value}"),
    };
    written.map_err(|_| Error::new(ErrorKind::WriteFailure, "出力に失敗しました"))
}

/// Jinja2 の `tojson` (`htmlsafe_json_dumps`)
fn tojson(value: Value) -> Result<String, Error> {
    let json = serde_json::to_value(&value).map_err(|e| {
        Error::new(
            ErrorKind::BadSerialization,
            format!("tojson に渡せない値です: {e}"),
        )
    })?;
    Ok(pyjson::dumps(&sort_keys(json))
        .replace('<', "\\u003c")
        .replace('>', "\\u003e")
        .replace('&', "\\u0026")
        .replace('\'', "\\u0027"))
}

/// Jinja2 の `indent(width, first=False, blank=False)`。
///
/// Jinja2 は入力の末尾の改行を残す (minijinja の `indent` は落とす) ので、末尾が
/// 改行の値 (PEM 等) では後ろに空行が 1 つ出る。その空行まで Python 版に合わせる
fn indent(value: String, width: usize, kwargs: Kwargs) -> Result<String, Error> {
    let first = kwargs.get::<Option<bool>>("first")?.unwrap_or(false);
    let blank = kwargs.get::<Option<bool>>("blank")?.unwrap_or(false);
    kwargs.assert_all_used()?;
    let indention = " ".repeat(width);
    // Jinja2 と同じく改行を 1 つ足してから行に分ける (末尾が改行なら空の行が残る)
    let text = value + "\n";
    let lines = split_lines(&text);
    let mut out = String::new();
    for (i, line) in lines.iter().enumerate() {
        if i > 0 {
            out.push('\n');
        }
        if (i > 0 || first) && (blank || !line.is_empty()) {
            out.push_str(&indention);
        }
        out.push_str(line);
    }
    Ok(out)
}

/// `json.dumps(sort_keys=True)` と同じ並び (key の code point 順)
fn sort_keys(value: Json) -> Json {
    match value {
        Json::Object(map) => {
            let mut entries: Vec<(String, Json)> =
                map.into_iter().map(|(k, v)| (k, sort_keys(v))).collect();
            entries.sort_by(|a, b| a.0.cmp(&b.0));
            Json::Object(entries.into_iter().collect())
        }
        Json::Array(items) => Json::Array(items.into_iter().map(sort_keys).collect()),
        other => other,
    }
}

/// Python 版の後処理: `splitlines()` して、`strip()` が `#` / `# prettier-ignore` の
/// 行を除き、`"\n".join` する (末尾の改行は残らない)
fn drop_control_lines(rendered: &str) -> String {
    split_lines(rendered)
        .into_iter()
        .filter(|line| !matches!(line.trim(), "#" | "# prettier-ignore"))
        .collect::<Vec<_>>()
        .join("\n")
}

/// Python の `str.splitlines()` (行の区切りは `\n` 以外も含む)
fn split_lines(text: &str) -> Vec<&str> {
    const BREAKS: [char; 10] = [
        '\n', '\r', '\u{0b}', '\u{0c}', '\u{1c}', '\u{1d}', '\u{1e}', '\u{85}', '\u{2028}',
        '\u{2029}',
    ];
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        if !BREAKS.contains(&c) {
            continue;
        }
        lines.push(&text[start..i]);
        start = i + c.len_utf8();
        if c == '\r' {
            if let Some(&(j, '\n')) = chars.peek() {
                chars.next();
                start = j + 1;
            }
        }
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stack::testing::{each_stack, read_json, read_text};

    #[test]
    fn test_split_lines_matches_python() {
        assert_eq!(split_lines("a\nb\r\nc\rd\n"), ["a", "b", "c", "d"]);
        assert_eq!(split_lines("a\n\nb"), ["a", "", "b"]);
        assert_eq!(split_lines(""), Vec::<&str>::new());
        assert_eq!(split_lines("a\u{2028}b"), ["a", "b"]);
    }

    #[test]
    fn test_indent_matches_jinja2() {
        let kwargs = |first: bool| Kwargs::from_iter([("first", Value::from(first))]);
        // Python: jinja2.filters.do_indent("a\nb\n", 2) == "a\n  b\n"
        assert_eq!(
            indent("a\nb\n".into(), 2, kwargs(false)).unwrap(),
            "a\n  b\n"
        );
        assert_eq!(
            indent("a\n\nb".into(), 2, kwargs(false)).unwrap(),
            "a\n\n  b"
        );
        assert_eq!(indent("a\nb".into(), 2, kwargs(true)).unwrap(), "  a\n  b");
    }

    #[test]
    fn test_tojson_matches_jinja2() {
        let value = Value::from(Serde(
            &serde_json::json!({"b": "<a&'写>", "a": [1, true, null]}),
        ));
        assert_eq!(
            tojson(value).unwrap(),
            "{\"a\": [1, true, null], \"b\": \"\\u003ca\\u0026\\u0027\\u5199\\u003e\"}"
        );
    }

    /// Python が template に渡した変数 (`*.input.json`) をそのまま描画すると、
    /// Python の描画結果とバイト一致する (minijinja と Jinja2 の差が無いこと)
    #[test]
    fn test_render_matches_python_with_python_variables() {
        let mut count = 0;
        each_stack(|dir, stack| {
            let Some(input_file) = stack["input_file"].as_str() else {
                return;
            };
            let kind = stack["kind"].as_str().expect("kind");
            let template = match kind {
                "acm" => "cloudfront_acm",
                "waf" => "cloudfront_waf",
                other => other,
            };
            let variables = read_json(&dir.join(input_file));
            let want = read_text(&dir.join(stack["file"].as_str().expect("file")));
            let got = render_template(template, &variables)
                .unwrap_or_else(|e| panic!("{}: {e:#}", dir.join(input_file).display()));
            assert!(
                got.clone() + "\n" == want,
                "{} の描画が Python と違います:\n{}",
                dir.join(input_file).display(),
                first_difference(&got, &want)
            );
            count += 1;
        });
        assert!(count > 100, "vector が少なすぎます: {count}");
    }

    fn first_difference(got: &str, want: &str) -> String {
        for (i, (g, w)) in got.lines().zip(want.lines()).enumerate() {
            if g != w {
                return format!("{} 行目\n  rust:   {g:?}\n  python: {w:?}", i + 1);
            }
        }
        format!(
            "行数が違います (rust {} / python {})",
            got.lines().count(),
            want.lines().count()
        )
    }
}
