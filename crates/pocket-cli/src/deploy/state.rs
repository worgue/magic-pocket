//! state bucket の `resources.json` (Python 版 `resources/aws/state.py` の `StateStore`)。
//!
//! deploy がリソースごとの記録 (stack 名・bucket 名など) を書く。宣言から消えたリソースの
//! 検出 (inbound の残存 stack・未使用の受信ドメイン) がこの記録を読む。JSON の形は
//! Python 版と同じで、どちらの CLI が書いた state も相互に読める

use anyhow::{bail, Context as _, Result};
use aws_config::SdkConfig;
use aws_sdk_s3::primitives::ByteStream;
use magic_pocket_settings::context::Context;
use magic_pocket_settings::pyjson;
use serde_json::{json, Value as Json};

use crate::aws_util::{has_code, regional_config, sdk_error};
use crate::stack::roles;

const STATE_KEY: &str = "resources.json";

/// まだ何も記録していない state
pub fn empty_state() -> Json {
    json!({"version": 1, "resources": {}})
}

/// dict は再帰的に merge し、list と値は上書きする (Python の `_deep_merge`)
pub fn deep_merge(base: &mut Json, info: &Json) {
    let (Some(base_map), Some(info_map)) = (base.as_object_mut(), info.as_object()) else {
        *base = info.clone();
        return;
    };
    for (key, value) in info_map {
        match base_map.get_mut(key) {
            Some(existing) if existing.is_object() && value.is_object() => {
                deep_merge(existing, value);
            }
            _ => {
                base_map.insert(key.clone(), value.clone());
            }
        }
    }
}

/// 保存するテキスト (Python の `json.dumps(state, indent=2)`)
pub fn render(state: &Json) -> String {
    pyjson::dumps_indent2(state)
}

/// 読んだテキストを state にする。壊れた JSON・`resources` が dict でない state は
/// 空として扱わずに止める (空で上書きすると、残存リソースの検出が効かなくなる)
pub fn parse(bucket: &str, text: &str) -> Result<Json> {
    let state: Json = serde_json::from_str(text)
        .with_context(|| format!("s3://{bucket}/{STATE_KEY} が JSON ではありません"))?;
    if !state["resources"].is_object() {
        bail!("s3://{bucket}/{STATE_KEY} に resources (dict) がありません");
    }
    Ok(state)
}

pub struct StateStore {
    client: aws_sdk_s3::Client,
    pub bucket_name: String,
    pub region: String,
}

impl StateStore {
    pub fn new(sdk: &SdkConfig, ctx: &Context) -> Result<Self> {
        let general = ctx
            .general
            .as_ref()
            .context("general context is not configured")?;
        let bucket_name =
            roles::state_bucket_name(ctx).context("general context is not configured")?;
        Ok(Self {
            client: aws_sdk_s3::Client::new(&regional_config(sdk, &general.region)),
            bucket_name,
            region: general.region.clone(),
        })
    }

    /// state bucket が無ければ作る。別の region に在ればエラー
    pub async fn ensure_bucket(&self) -> Result<()> {
        crate::image_build::ensure_state_bucket(&self.client, &self.bucket_name, &self.region).await
    }

    /// state を読む。object (または bucket) が無いときだけ空の state を返し、
    /// 権限不足などの error はそのまま止める
    pub async fn load(&self) -> Result<Json> {
        let response = self
            .client
            .get_object()
            .bucket(&self.bucket_name)
            .key(STATE_KEY)
            .send()
            .await;
        let response = match response {
            Ok(response) => response,
            Err(e) if has_code(&e, &["NoSuchKey"]) => return Ok(empty_state()),
            Err(e) => return Err(sdk_error("s3 get_object (state)", &e)),
        };
        let body = response
            .body
            .collect()
            .await
            .context("state を読めません")?;
        let text = String::from_utf8(body.into_bytes().to_vec())
            .context("state が UTF-8 ではありません")?;
        parse(&self.bucket_name, &text)
    }

    /// `info` を `resources` に merge して保存する (記録するものが無くても保存する。
    /// Python 版と同じく、最初の create で state の object ができる)
    pub async fn record(&self, info: &Json) -> Result<()> {
        let mut state = self.load().await?;
        deep_merge(&mut state["resources"], info);
        self.client
            .put_object()
            .bucket(&self.bucket_name)
            .key(STATE_KEY)
            .body(ByteStream::from(render(&state).into_bytes()))
            .content_type("application/json")
            .send()
            .await
            .map_err(|e| sdk_error("s3 put_object (state)", &e))?;
        Ok(())
    }

    /// state bucket を中身ごと消す。無ければ `false`
    pub async fn delete_bucket(&self) -> Result<bool> {
        super::s3::delete_bucket_with_contents(&self.client, &self.bucket_name).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_deep_merge_merges_dicts_and_replaces_the_rest() {
        let mut state = json!({
            "ecr": {"web": {"repository_name": "a"}},
            "backup": {"plans": ["daily", "weekly"]},
            "s3": {"bucket_name": "old"},
        });
        deep_merge(
            &mut state,
            &json!({
                "ecr": {"api": {"repository_name": "b"}},
                "backup": {"plans": ["daily"]},
                "s3": {"bucket_name": "new"},
                "inbound": {"support": {"stack_name": "s"}},
            }),
        );
        assert_eq!(
            state,
            json!({
                "ecr": {"web": {"repository_name": "a"}, "api": {"repository_name": "b"}},
                "backup": {"plans": ["daily"]},
                "s3": {"bucket_name": "new"},
                "inbound": {"support": {"stack_name": "s"}},
            })
        );
        // 記録するものが無い resource (空の dict) は state を変えない
        let before = state.clone();
        deep_merge(&mut state, &json!({}));
        assert_eq!(state, before);
    }

    #[test]
    fn test_render_matches_python_json_dumps() {
        let mut state = empty_state();
        deep_merge(
            &mut state["resources"],
            &json!({"s3": {"bucket_name": "日本"}}),
        );
        assert_eq!(
            render(&state),
            "{\n  \"version\": 1,\n  \"resources\": {\n    \"s3\": {\n      \
             \"bucket_name\": \"\\u65e5\\u672c\"\n    }\n  }\n}"
        );
    }

    #[test]
    fn test_parse_rejects_broken_state() {
        assert_eq!(parse("b", &render(&empty_state())).unwrap(), empty_state());
        let error = parse("b", "{not json").unwrap_err().to_string();
        assert_eq!(error, "s3://b/resources.json が JSON ではありません");
        let error = parse("b", "{\"version\": 1}").unwrap_err().to_string();
        assert_eq!(
            error,
            "s3://b/resources.json に resources (dict) がありません"
        );
        assert!(parse("b", "[]").is_err());
    }
}
