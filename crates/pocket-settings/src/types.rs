//! field の型の部品: 正規表現・長さの制約付き文字列と、素通しの値。

use std::sync::OnceLock;

use regex::Regex;
use serde::de::Error as _;
use serde::{Deserialize, Deserializer, Serialize};

/// 正規表現と最大長の制約付き文字列を定義する。pattern は Python の
/// `Field(pattern=...)` と同じ文字列を書く。
macro_rules! constrained_str {
    ($(#[$meta:meta])* $name:ident, $pattern:expr, $max_len:expr) => {
        $(#[$meta])*
        #[derive(Debug, Clone, PartialEq, Eq, Serialize)]
        #[serde(transparent)]
        pub struct $name(pub String);

        impl $name {
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl<'de> Deserialize<'de> for $name {
            fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
                static RE: OnceLock<Regex> = OnceLock::new();
                let s = String::deserialize(d)?;
                let re = RE.get_or_init(|| Regex::new($pattern).expect("pattern"));
                let max_len: Option<usize> = $max_len;
                if let Some(max) = max_len {
                    if s.chars().count() > max {
                        return Err(D::Error::custom(format!(
                            "String should have at most {max} characters"
                        )));
                    }
                }
                if !re.is_match(&s) {
                    return Err(D::Error::custom(format!(
                        "String should match pattern '{}'",
                        $pattern
                    )));
                }
                Ok(Self(s))
            }
        }
    };
}

constrained_str!(
    /// Docker tag として使える文字列 (Python の `TagStr`)
    TagStr,
    "^[a-z0-9][a-z0-9._-]*$",
    Some(128)
);

/// 型を問わない値 (Python の `dict[str, Any]` の値)。
///
/// toml の datetime は Python の `model_dump(mode="json")` と同じ ISO 文字列にする。
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(transparent)]
pub struct AnyValue(pub serde_json::Value);

impl<'de> Deserialize<'de> for AnyValue {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let v = toml::Value::deserialize(d)?;
        Ok(Self(toml_to_json(&v)))
    }
}

/// deserialize の前に datetime を置き換える table の key。`toml::Value` を
/// Deserializer として使うと datetime が普通の文字列になり見分けられないため、
/// 印付きの table にしておく (文字列の field に datetime を書いた場合は、Python の
/// strict と同じく型エラーになる)
const DATETIME_MARKER: &str = "$__pocket_datetime";

/// table 内の datetime を印付きの table (値は Python の JSON 形の文字列) に置き換える
pub(crate) fn mark_datetimes(v: &mut toml::Value) {
    match v {
        toml::Value::Datetime(dt) => {
            let mut t = toml::Table::new();
            t.insert(
                DATETIME_MARKER.to_string(),
                toml::Value::String(python_isoformat(dt)),
            );
            *v = toml::Value::Table(t);
        }
        toml::Value::Array(a) => a.iter_mut().for_each(mark_datetimes),
        toml::Value::Table(t) => t.iter_mut().for_each(|(_, v)| mark_datetimes(v)),
        _ => {}
    }
}

fn toml_to_json(v: &toml::Value) -> serde_json::Value {
    match v {
        toml::Value::Table(t) if t.len() == 1 && t.contains_key(DATETIME_MARKER) => t
            [DATETIME_MARKER]
            .clone()
            .try_into()
            .unwrap_or(serde_json::Value::Null),
        toml::Value::String(s) => serde_json::Value::String(s.clone()),
        toml::Value::Integer(i) => serde_json::Value::from(*i),
        // inf / nan は Python (pydantic) と同じく null になる
        toml::Value::Float(f) => serde_json::Number::from_f64(*f)
            .map(serde_json::Value::Number)
            .unwrap_or(serde_json::Value::Null),
        toml::Value::Boolean(b) => serde_json::Value::Bool(*b),
        toml::Value::Datetime(dt) => serde_json::Value::String(python_isoformat(dt)),
        toml::Value::Array(a) => serde_json::Value::Array(a.iter().map(toml_to_json).collect()),
        toml::Value::Table(t) => serde_json::Value::Object(
            t.iter()
                .map(|(k, v)| (k.clone(), toml_to_json(v)))
                .collect(),
        ),
    }
}

/// toml の datetime を pydantic の JSON 形にする。
///
/// 小数秒はマイクロ秒に切り捨てて 6 桁 (0 なら省く)、UTC は `Z`、区切りは `T`。
fn python_isoformat(dt: &toml::value::Datetime) -> String {
    let mut out = String::new();
    if let Some(d) = &dt.date {
        out.push_str(&format!("{:04}-{:02}-{:02}", d.year, d.month, d.day));
    }
    if let Some(t) = &dt.time {
        if dt.date.is_some() {
            out.push('T');
        }
        out.push_str(&format!(
            "{:02}:{:02}:{:02}",
            t.hour,
            t.minute,
            t.second.unwrap_or(0)
        ));
        let micros = t.nanosecond.unwrap_or(0) / 1000;
        if micros > 0 {
            out.push_str(&format!(".{micros:06}"));
        }
    }
    match &dt.offset {
        Some(toml::value::Offset::Z) => out.push('Z'),
        Some(toml::value::Offset::Custom { minutes }) => {
            if *minutes == 0 {
                out.push('Z');
            } else {
                let sign = if *minutes < 0 { '-' } else { '+' };
                let m = minutes.unsigned_abs();
                out.push_str(&format!("{sign}{:02}:{:02}", m / 60, m % 60));
            }
        }
        None => {}
    }
    out
}
