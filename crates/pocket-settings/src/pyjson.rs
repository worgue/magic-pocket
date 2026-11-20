//! Python の `json.dumps` と同じ文字列。`pocket settings` / `pocket context` の出力
//! (`indent=2`) と、Context の `input_json` (`ensure_ascii=False`、既定の区切り)
//! を Python 版と一致させるため。`ensure_ascii` では非 ASCII を `\uXXXX`
//! (BMP 外はサロゲートペア) にし、小数は Python の `repr(float)` と同じ形にする。

use serde_json::Value;

/// `json.dumps(value, indent=2)`
pub fn dumps_indent2(value: &Value) -> String {
    let mut out = String::new();
    write_value(value, Some(0), true, &mut out);
    out
}

/// `json.dumps(value)` (1 行、`ensure_ascii`、区切りは `", "` / `": "`)
pub fn dumps(value: &Value) -> String {
    let mut out = String::new();
    write_value(value, None, true, &mut out);
    out
}

/// `json.dumps(value, ensure_ascii=False)` (区切りは `", "` / `": "`)
pub fn dumps_unicode(value: &Value) -> String {
    let mut out = String::new();
    write_value(value, None, false, &mut out);
    out
}

/// `json.dumps(value, ensure_ascii=False, indent=2)`
pub fn dumps_indent2_unicode(value: &Value) -> String {
    let mut out = String::new();
    write_value(value, Some(0), false, &mut out);
    out
}

/// `level` が `None` なら 1 行 (indent なし)
fn write_value(value: &Value, level: Option<usize>, ascii: bool, out: &mut String) {
    let inner = level.map(|l| l + 1);
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                out.push_str(&i.to_string());
            } else if let Some(u) = n.as_u64() {
                out.push_str(&u.to_string());
            } else {
                out.push_str(&python_float_repr(n.as_f64().unwrap_or(f64::NAN)));
            }
        }
        Value::String(s) => write_string(s, ascii, out),
        Value::Array(items) => {
            if items.is_empty() {
                out.push_str("[]");
                return;
            }
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                separator(i, inner, out);
                write_value(item, inner, ascii, out);
            }
            newline(level, out);
            out.push(']');
        }
        Value::Object(map) => {
            if map.is_empty() {
                out.push_str("{}");
                return;
            }
            out.push('{');
            for (i, (k, v)) in map.iter().enumerate() {
                separator(i, inner, out);
                write_string(k, ascii, out);
                out.push_str(": ");
                write_value(v, inner, ascii, out);
            }
            newline(level, out);
            out.push('}');
        }
    }
}

/// 要素の前の区切り。indent ありなら `,` + 改行、なしなら `, `
fn separator(i: usize, level: Option<usize>, out: &mut String) {
    if i > 0 {
        out.push(',');
        if level.is_none() {
            out.push(' ');
        }
    }
    newline(level, out);
}

fn newline(level: Option<usize>, out: &mut String) {
    if let Some(level) = level {
        out.push('\n');
        out.push_str(&"  ".repeat(level));
    }
}

fn write_string(s: &str, ascii: bool, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (ascii && (c as u32) > 0x7e) => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// Python の `repr(float)`: 最短で往復できる桁。指数が -4 未満か 16 以上なら
/// `1e+20` / `1.5e-05` の形、それ以外は小数点付きの位取り (`1.0` / `0.0001`)
fn python_float_repr(f: f64) -> String {
    if f.is_nan() {
        return "NaN".to_string();
    }
    if f.is_infinite() {
        return if f > 0.0 { "Infinity" } else { "-Infinity" }.to_string();
    }
    if f == 0.0 {
        return if f.is_sign_negative() { "-0.0" } else { "0.0" }.to_string();
    }
    // `{:e}` は最短の桁: "1.5e-5" / "1e20"
    let sci = format!("{f:e}");
    let (mantissa, exp) = sci.split_once('e').expect("指数表記");
    let exp: i32 = exp.parse().expect("指数");
    let negative = mantissa.starts_with('-');
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let sign = if negative { "-" } else { "" };
    if !(-4..16).contains(&exp) {
        let (head, tail) = digits.split_at(1);
        let frac = if tail.is_empty() {
            String::new()
        } else {
            format!(".{tail}")
        };
        let esign = if exp < 0 { '-' } else { '+' };
        return format!("{sign}{head}{frac}e{esign}{:02}", exp.abs());
    }
    let point = exp + 1; // 小数点の位置 (digits の先頭からの桁数)
    let body = if point <= 0 {
        format!("0.{}{}", "0".repeat((-point) as usize), digits)
    } else if (point as usize) >= digits.len() {
        format!("{}{}.0", digits, "0".repeat(point as usize - digits.len()))
    } else {
        let (int, frac) = digits.split_at(point as usize);
        format!("{int}.{frac}")
    };
    format!("{sign}{body}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_float_repr_matches_python() {
        for (f, want) in [
            (1.0, "1.0"),
            (1.5, "1.5"),
            (0.5, "0.5"),
            (2.0, "2.0"),
            (100.0, "100.0"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (1e16, "1e+16"),
            (1e20, "1e+20"),
            (123456789012345.6, "123456789012345.6"),
            (-8.5, "-8.5"),
            (1.25e-7, "1.25e-07"),
            (-0.0, "-0.0"),
        ] {
            assert_eq!(python_float_repr(f), want, "{f}");
        }
    }

    #[test]
    fn test_dumps_matches_python_layout() {
        let v: Value = serde_json::from_str(
            r#"{"a": [1, {"b": "写真 😀"}], "e": {}, "l": [], "n": null, "t": true}"#,
        )
        .unwrap();
        assert_eq!(
            dumps_indent2(&v),
            "{\n  \"a\": [\n    1,\n    {\n      \"b\": \"\\u5199\\u771f \\ud83d\\ude00\"\n    }\n  ],\n  \
             \"e\": {},\n  \"l\": [],\n  \"n\": null,\n  \"t\": true\n}"
        );
        assert_eq!(
            dumps(&v),
            "{\"a\": [1, {\"b\": \"\\u5199\\u771f \\ud83d\\ude00\"}], \"e\": {}, \"l\": [], \"n\": null, \"t\": true}"
        );
        assert_eq!(
            dumps_unicode(&v),
            "{\"a\": [1, {\"b\": \"写真 😀\"}], \"e\": {}, \"l\": [], \"n\": null, \"t\": true}"
        );
    }
}
