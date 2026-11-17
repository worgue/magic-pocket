//! CloudFormation stack (template の描画と stack の操作)。
//!
//! Python 版 `pocket_cli/resources/aws/cloudformation.py` の移植。stack の
//! `pocket:template_hash` タグは template のテキストの hash なので、描画結果は
//! Python 版とバイト一致させる (`tests/data/template_vectors` で固定している)。
//!
//! template を組む関数は純粋 (AWS を呼ばない)。他 stack の output や secret の値など
//! 外から引く値は、呼び出し側が解決して引数で渡す。

pub mod cloudfront;
pub mod container;
pub mod inbound;
pub mod ops;
pub mod render;
pub mod roles;
pub mod vpc;

use std::collections::BTreeMap;

use serde_json::{Map, Value as Json};
use sha2::{Digest, Sha256};

/// stack の Outputs (OutputKey → 値、Export 名 → 値の両方で引ける。Python 版 `Stack.output`)
pub type StackOutputs = BTreeMap<String, String>;

/// 描画済みの stack
#[derive(Debug, Clone, PartialEq)]
pub struct StackTemplate {
    /// stack 名
    pub name: String,
    /// stack を置く region
    pub region: String,
    pub capabilities: Vec<String>,
    /// stack に付けるタグ (`pocket:template_hash` を除く)
    pub tags: Vec<(String, String)>,
    /// この stack が公開する Export 名 (template 内の名前 → Export 名)
    pub export: Map<String, Json>,
    /// template のテキスト (`create_stack` / `update_stack` の TemplateBody)
    pub body: String,
}

impl StackTemplate {
    /// `pocket:template_hash` タグの値 (テキストの sha256 の先頭 16 桁)
    pub fn template_hash(&self) -> String {
        let digest = Sha256::digest(self.body.as_bytes());
        let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
        hex[..16].to_string()
    }
}

/// template に渡す変数 (Python 版 `Stack._render_input`)。同じ key は Context の値が勝つ
fn variables(
    stack_name: &str,
    export: &Map<String, Json>,
    extra: Map<String, Json>,
    context: Json,
) -> Json {
    let mut vars = Map::new();
    vars.insert("stack_name".into(), Json::String(stack_name.to_string()));
    vars.insert("export".into(), Json::Object(export.clone()));
    vars.extend(extra);
    if let Json::Object(context) = context {
        vars.extend(context);
    }
    Json::Object(vars)
}

/// Context の struct を `model_dump()` と同じ JSON にする
fn dump<T: serde::Serialize>(context: &T) -> Json {
    serde_json::to_value(context).expect("Context は JSON にできる")
}

#[cfg(test)]
mod testing;
#[cfg(test)]
mod vector_tests;
