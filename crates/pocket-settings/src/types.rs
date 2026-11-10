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

constrained_str!(
    /// 変数 (`{stage}` 等) を含められる名前の書式 (Python の `FormatStr`)
    FormatStr,
    "^[a-z0-9{][{}a-z0-9._-]*$",
    Some(128)
);

constrained_str!(
    /// EFS の mount 先 (`/mnt/` 配下)
    EfsMountPath,
    "^/mnt/.*",
    None
);

constrained_str!(
    /// EFS の access point の path
    EfsAccessPointPath,
    "^/.+",
    None
);

constrained_str!(
    /// AZ の末尾 1 文字 (Vpc.zone_suffixes の要素)
    ZoneSuffix,
    "^",
    Some(1)
);

impl FormatStr {
    /// `{namespace}` / `{stage}` / `{project}` 等を展開する
    pub fn format(&self, vars: &[(&str, &str)]) -> String {
        let mut out = self.0.clone();
        for (k, v) in vars {
            out = out.replace(&format!("{{{k}}}"), v);
        }
        out
    }
}

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

/// 範囲制約付きの整数を定義する (Python の `Field(ge=..., le=...)`)。
/// 範囲外は型エラーとして扱う (error vector では message を比べない)
macro_rules! ranged_int_fn {
    ($name:ident, $opt:ident, $ge:expr, $le:expr) => {
        #[allow(dead_code)]
        pub(crate) fn $name<'de, D: Deserializer<'de>>(d: D) -> Result<i64, D::Error> {
            let v = i64::deserialize(d)?;
            check_range(v, $ge, $le).map_err(D::Error::custom)?;
            Ok(v)
        }

        #[allow(dead_code)]
        pub(crate) fn $opt<'de, D: Deserializer<'de>>(d: D) -> Result<Option<i64>, D::Error> {
            let v = i64::deserialize(d)?;
            check_range(v, $ge, $le).map_err(D::Error::custom)?;
            Ok(Some(v))
        }
    };
}

fn check_range(v: i64, ge: Option<i64>, le: Option<i64>) -> Result<(), String> {
    if let Some(ge) = ge {
        if v < ge {
            return Err(format!("Input should be greater than or equal to {ge}"));
        }
    }
    if let Some(le) = le {
        if v > le {
            return Err(format!("Input should be less than or equal to {le}"));
        }
    }
    Ok(())
}

ranged_int_fn!(ge1, opt_ge1, Some(1), None);

/// toml に書いた順を保つ map (Python の dict と同じ順で検証し、エラー文の
/// 「最初に見つかったもの」を一致させるため)。JSON には object として出す
#[derive(Debug, Clone)]
pub struct OrderedMap<V>(pub Vec<(String, V)>);

/// 等値は Python の dict と同じく順序を見ない
impl<V: PartialEq> PartialEq for OrderedMap<V> {
    fn eq(&self, other: &Self) -> bool {
        self.len() == other.len() && self.iter().all(|(k, v)| other.get(k) == Some(v))
    }
}

impl<V> Default for OrderedMap<V> {
    fn default() -> Self {
        Self(Vec::new())
    }
}

impl<V> OrderedMap<V> {
    pub fn get(&self, key: &str) -> Option<&V> {
        self.0.iter().find(|(k, _)| k == key).map(|(_, v)| v)
    }

    pub fn contains_key(&self, key: &str) -> bool {
        self.get(key).is_some()
    }

    pub fn iter(&self) -> impl Iterator<Item = (&String, &V)> {
        self.0.iter().map(|(k, v)| (k, v))
    }

    pub fn iter_mut(&mut self) -> impl Iterator<Item = (&String, &mut V)> {
        self.0.iter_mut().map(|(k, v)| (&*k, v))
    }

    pub fn keys(&self) -> impl Iterator<Item = &String> {
        self.0.iter().map(|(k, _)| k)
    }

    pub fn values(&self) -> impl Iterator<Item = &V> {
        self.0.iter().map(|(_, v)| v)
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    pub fn len(&self) -> usize {
        self.0.len()
    }
}

impl<'de, V: Deserialize<'de>> Deserialize<'de> for OrderedMap<V> {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct Visitor<V>(std::marker::PhantomData<V>);
        impl<'de, V: Deserialize<'de>> serde::de::Visitor<'de> for Visitor<V> {
            type Value = OrderedMap<V>;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("a table")
            }
            fn visit_map<A: serde::de::MapAccess<'de>>(
                self,
                mut map: A,
            ) -> Result<Self::Value, A::Error> {
                let mut out = Vec::new();
                while let Some((k, v)) = map.next_entry::<String, V>()? {
                    out.push((k, v));
                }
                Ok(OrderedMap(out))
            }
        }
        d.deserialize_map(Visitor(std::marker::PhantomData))
    }
}

impl<V: Serialize> Serialize for OrderedMap<V> {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        use serde::ser::SerializeMap;
        let mut m = s.serialize_map(Some(self.0.len()))?;
        for (k, v) in &self.0 {
            m.serialize_entry(k, v)?;
        }
        m.end()
    }
}

/// 型を問わない table (Python の型なし `dict`)。table 以外は型エラー
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(transparent)]
pub struct AnyDict(pub serde_json::Value);

impl<'de> Deserialize<'de> for AnyDict {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let v = toml::Value::deserialize(d)?;
        match &v {
            toml::Value::Table(t) if !(t.len() == 1 && t.contains_key(DATETIME_MARKER)) => {
                Ok(Self(toml_to_json(&v)))
            }
            _ => Err(D::Error::custom("Input should be a valid dictionary")),
        }
    }
}

/// 文字列か整数 (Python の `str | int`。strict なので bool・小数は型エラー)
#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(untagged)]
pub enum StrOrInt {
    Str(String),
    Int(i64),
}

ranged_int_fn!(waf_limit, opt_waf_limit, Some(10), Some(2_000_000_000));

/// Python の `repr(str)` (エラー文の `%r`)。quote は `'`、`'` を含み `"` を含まなければ `"`
pub(crate) fn py_repr(s: &str) -> String {
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

ranged_int_fn!(ge0, opt_ge0, Some(0), None);
ranged_int_fn!(retention_1_35, opt_retention_1_35, Some(1), Some(35));
ranged_int_fn!(ge14, opt_ge14, Some(14), None);

constrained_str!(
    /// RDS のパスワードローテーションの schedule (`cron(...)` / `rate(...)`)
    RotationSchedule,
    r"^(cron|rate)\(.+\)$",
    None
);

constrained_str!(
    /// RDS のパスワードローテーションの所要上限 (`1h`〜`24h`)
    RotationDuration,
    "^([1-9]|1[0-9]|2[0-4])h$",
    None
);
