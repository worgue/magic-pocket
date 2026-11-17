//! Lambda function の書き込み (Python 版 `pocket_cli/resources/aws/lambdahandler.py` の
//! update / wait_update / 環境変数の更新と、`Container.ensure_post_deploy_state`)。

use std::collections::BTreeMap;
use std::io::Write;
use std::time::Duration;

use anyhow::{anyhow, bail, Result};
use magic_pocket_settings::context::ContainerContext;

use crate::aws_util::{has_code, regional_config, sdk_error};
use crate::stack::ops::ResourceStatus;
use crate::status::container::{handler_status, HandlerState};
use crate::status::sink::Sink;

/// `update_function_code` の応答のうち表示する項目
#[derive(Debug, Clone, Default, PartialEq)]
pub struct UpdatedFunction {
    pub function_name: Option<String>,
    pub timeout: Option<i32>,
    pub memory_size: Option<i32>,
    pub version: Option<String>,
    pub environment: Option<BTreeMap<String, String>>,
}

/// code の更新の失敗
#[derive(Debug)]
pub enum UpdateCodeError {
    /// 別の更新が進行中 (`ResourceConflictException`)
    Conflict,
    Other(anyhow::Error),
}

pub trait LambdaApi {
    /// function の状態。無ければ両方 `None`
    async fn state(&self, name: &str) -> Result<HandlerState>;
    async fn update_code(
        &self,
        name: &str,
        image_uri: &str,
    ) -> std::result::Result<UpdatedFunction, UpdateCodeError>;
    /// 今の環境変数 (ImportValue / secret は解決済みの実値)。function が無ければ空
    async fn environment(&self, name: &str) -> Result<BTreeMap<String, String>>;
    async fn update_environment(&self, name: &str, env: &BTreeMap<String, String>) -> Result<()>;
}

/// 更新の完了待ちの間隔と回数 (Python 版は 3 秒 × 20 回)
#[derive(Debug, Clone, Copy)]
pub struct WaitUpdate {
    pub interval: Duration,
    pub attempts: u32,
}

impl Default for WaitUpdate {
    fn default() -> Self {
        Self {
            interval: Duration::from_secs(3),
            attempts: 20,
        }
    }
}

pub async fn status(api: &impl LambdaApi, name: &str) -> Result<ResourceStatus> {
    handler_status(&api.state(name).await?)
}

/// function の image を差し替える (環境変数は変えない)
pub async fn update_code(
    api: &impl LambdaApi,
    sink: &mut dyn Sink,
    name: &str,
    image_uri: &str,
) -> Result<()> {
    let updated = match api.update_code(name, image_uri).await {
        Ok(updated) => updated,
        // 並行する deploy が同じ function を更新中。原因と次の一手を文言に含める
        Err(UpdateCodeError::Conflict) => bail!(
            "Lambda function '{name}' が別の更新処理中のため、\
             code を更新できませんでした。同じ stage の `pocket deploy` を\
             並行実行していないか確認し、実行中ならその完了を待ってから\
             再実行してください (deploy は数分かかります)。"
        ),
        Err(UpdateCodeError::Other(e)) => return Err(e),
    };
    sink.print(&format!("lambda function {name} was updated."));
    for line in updated_lines(&updated) {
        sink.print(&line);
    }
    Ok(())
}

fn updated_lines(updated: &UpdatedFunction) -> Vec<String> {
    let mut lines = Vec::new();
    if let Some(value) = &updated.function_name {
        lines.push(format!("  - FunctionName: {value}"));
    }
    if let Some(value) = updated.timeout {
        lines.push(format!("  - Timeout: {value}"));
    }
    if let Some(value) = updated.memory_size {
        lines.push(format!("  - MemorySize: {value}"));
    }
    if let Some(value) = &updated.version {
        lines.push(format!("  - Version: {value}"));
    }
    if let Some(variables) = &updated.environment {
        // Python 版は dict の repr。Rust 版は JSON で出す
        let value = serde_json::json!({ "Variables": variables });
        lines.push(format!("  - Environment: {value}"));
    }
    lines
}

/// 更新 (code / 環境変数) が終わるまで待つ。進行の表示は stdout に直接出す
pub async fn wait_update(api: &impl LambdaApi, name: &str, wait: WaitUpdate) -> Result<()> {
    let mut out = std::io::stdout();
    print!("waiting lambda function {name} update.");
    out.flush().ok();
    for _ in 0..wait.attempts {
        tokio::time::sleep(wait.interval).await;
        match status(api, name).await? {
            ResourceStatus::Progress => {
                print!(".");
                out.flush().ok();
            }
            ResourceStatus::Failed => {
                println!();
                bail!(
                    "lambda function {name} の更新が失敗しました \
                     (LastUpdateStatus=Failed)。image URI や設定を確認してください。"
                );
            }
            _ => {
                println!("\nlambda function {name} was updated.");
                return Ok(());
            }
        }
    }
    Err(anyhow!("Lambda couldn't stop updating. Please check."))
}

/// 環境変数を直接更新する (CloudFormation を通さない)
pub async fn update_environment(
    api: &impl LambdaApi,
    sink: &mut dyn Sink,
    name: &str,
    env: &BTreeMap<String, String>,
    wait: WaitUpdate,
) -> Result<()> {
    api.update_environment(name, env).await?;
    sink.print(&format!("lambda function {name} environment was updated."));
    wait_update(api, name, wait).await
}

/// deploy の最後に、各 function の環境変数 DEPLOY_HASH を今回の値に揃える。
///
/// code の更新は環境変数を変えず、環境変数は stack の更新でしか書き換わらない。stack が
/// 更新不要と判定されると DEPLOY_HASH が古いまま残り、古い hash の static URL を返して
/// しまうので、stack の状態に依らずここで揃える
pub async fn sync_deploy_hash(
    api: &impl LambdaApi,
    sink: &mut dyn Sink,
    c: &ContainerContext,
    wait: WaitUpdate,
) -> Result<()> {
    let Some(deploy_hash) = c.envs.get("DEPLOY_HASH").filter(|hash| !hash.is_empty()) else {
        return Ok(());
    };
    for (key, handler) in c.handlers.iter() {
        let name = &handler.function_name;
        if status(api, name).await? == ResourceStatus::NoExist {
            continue;
        }
        let mut env = api.environment(name).await?;
        let current = env.get("DEPLOY_HASH").cloned();
        if current.as_deref() == Some(deploy_hash.as_str()) {
            continue;
        }
        sink.log(&format!(
            "[{key}] Lambda env DEPLOY_HASH を同期します ({} → {deploy_hash})",
            current.as_deref().unwrap_or("None")
        ));
        env.insert("DEPLOY_HASH".to_string(), deploy_hash.clone());
        update_environment(api, sink, name, &env, wait).await?;
    }
    Ok(())
}

/// AWS の Lambda
pub struct AwsLambda {
    client: aws_sdk_lambda::Client,
}

impl AwsLambda {
    pub fn new(sdk: &aws_config::SdkConfig, region: &str) -> Self {
        Self {
            client: aws_sdk_lambda::Client::new(&regional_config(sdk, region)),
        }
    }
}

impl LambdaApi for AwsLambda {
    async fn state(&self, name: &str) -> Result<HandlerState> {
        let response = match self.client.get_function().function_name(name).send().await {
            Ok(response) => response,
            Err(e) if has_code(&e, &["ResourceNotFoundException"]) => {
                return Ok(HandlerState::default())
            }
            Err(e) => return Err(sdk_error("lambda get_function", &e)),
        };
        let configuration = response.configuration();
        Ok(HandlerState {
            last_update_status: configuration
                .and_then(|c| c.last_update_status())
                .map(|status| status.as_str().to_string()),
            code_sha256: configuration
                .and_then(|c| c.code_sha256())
                .map(str::to_string),
        })
    }

    async fn update_code(
        &self,
        name: &str,
        image_uri: &str,
    ) -> std::result::Result<UpdatedFunction, UpdateCodeError> {
        let response = match self
            .client
            .update_function_code()
            .function_name(name)
            .image_uri(image_uri)
            .send()
            .await
        {
            Ok(response) => response,
            Err(e) if has_code(&e, &["ResourceConflictException"]) => {
                return Err(UpdateCodeError::Conflict)
            }
            Err(e) => {
                return Err(UpdateCodeError::Other(sdk_error(
                    "lambda update_function_code",
                    &e,
                )))
            }
        };
        Ok(UpdatedFunction {
            function_name: response.function_name().map(str::to_string),
            timeout: response.timeout(),
            memory_size: response.memory_size(),
            version: response.version().map(str::to_string),
            environment: response.environment().map(|environment| {
                environment
                    .variables()
                    .map(|variables| {
                        variables
                            .iter()
                            .map(|(k, v)| (k.clone(), v.clone()))
                            .collect()
                    })
                    .unwrap_or_default()
            }),
        })
    }

    async fn environment(&self, name: &str) -> Result<BTreeMap<String, String>> {
        let response = match self
            .client
            .get_function_configuration()
            .function_name(name)
            .send()
            .await
        {
            Ok(response) => response,
            Err(e) if has_code(&e, &["ResourceNotFoundException"]) => return Ok(BTreeMap::new()),
            Err(e) => return Err(sdk_error("lambda get_function_configuration", &e)),
        };
        Ok(response
            .environment()
            .and_then(|environment| environment.variables())
            .map(|variables| {
                variables
                    .iter()
                    .map(|(k, v)| (k.clone(), v.clone()))
                    .collect()
            })
            .unwrap_or_default())
    }

    async fn update_environment(&self, name: &str, env: &BTreeMap<String, String>) -> Result<()> {
        let environment = aws_sdk_lambda::types::Environment::builder()
            .set_variables(Some(
                env.iter().map(|(k, v)| (k.clone(), v.clone())).collect(),
            ))
            .build();
        self.client
            .update_function_configuration()
            .function_name(name)
            .environment(environment)
            .send()
            .await
            .map_err(|e| sdk_error("lambda update_function_configuration", &e))?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;
    use std::collections::VecDeque;

    use super::*;
    use crate::stack::testing::context_from_toml;
    use crate::status::sink::{Stream, Style};

    /// function ごとに、状態の列 (呼ぶたびに 1 つ進む。最後は居残る) と環境変数を持つ fake
    #[derive(Default)]
    struct FakeLambda {
        states: RefCell<BTreeMap<String, VecDeque<Option<&'static str>>>>,
        env: RefCell<BTreeMap<String, BTreeMap<String, String>>>,
        conflict: bool,
        calls: RefCell<Vec<String>>,
    }

    impl FakeLambda {
        fn with_states(self, name: &str, states: &[Option<&'static str>]) -> Self {
            self.states
                .borrow_mut()
                .insert(name.to_string(), states.iter().copied().collect());
            self
        }

        fn with_env(self, name: &str, pairs: &[(&str, &str)]) -> Self {
            self.env.borrow_mut().insert(
                name.to_string(),
                pairs
                    .iter()
                    .map(|(k, v)| (k.to_string(), v.to_string()))
                    .collect(),
            );
            self
        }
    }

    impl LambdaApi for FakeLambda {
        async fn state(&self, name: &str) -> Result<HandlerState> {
            let mut states = self.states.borrow_mut();
            let status = match states.get_mut(name) {
                Some(queue) if queue.len() > 1 => queue.pop_front().flatten(),
                Some(queue) => queue.front().copied().flatten(),
                None => None,
            };
            Ok(HandlerState {
                last_update_status: status.map(str::to_string),
                code_sha256: None,
            })
        }

        async fn update_code(
            &self,
            name: &str,
            image_uri: &str,
        ) -> std::result::Result<UpdatedFunction, UpdateCodeError> {
            if self.conflict {
                return Err(UpdateCodeError::Conflict);
            }
            self.calls
                .borrow_mut()
                .push(format!("update_code {name} {image_uri}"));
            Ok(UpdatedFunction {
                function_name: Some(name.to_string()),
                timeout: Some(30),
                memory_size: Some(512),
                version: Some("$LATEST".to_string()),
                environment: Some(BTreeMap::from([("A".to_string(), "1".to_string())])),
            })
        }

        async fn environment(&self, name: &str) -> Result<BTreeMap<String, String>> {
            Ok(self.env.borrow().get(name).cloned().unwrap_or_default())
        }

        async fn update_environment(
            &self,
            name: &str,
            env: &BTreeMap<String, String>,
        ) -> Result<()> {
            let pairs: Vec<String> = env.iter().map(|(k, v)| format!("{k}={v}")).collect();
            self.calls
                .borrow_mut()
                .push(format!("update_environment {name} {}", pairs.join(",")));
            self.env.borrow_mut().insert(name.to_string(), env.clone());
            Ok(())
        }
    }

    #[derive(Default)]
    struct Recorder(Vec<(Stream, Style, String)>);

    impl Sink for Recorder {
        fn emit(&mut self, stream: Stream, style: Style, text: &str) {
            self.0.push((stream, style, text.to_string()));
        }
    }

    fn quick(attempts: u32) -> WaitUpdate {
        WaitUpdate {
            interval: Duration::from_millis(1),
            attempts,
        }
    }

    #[tokio::test]
    async fn test_update_code_prints_summary() {
        let api = FakeLambda::default();
        let mut sink = Recorder::default();
        update_code(&api, &mut sink, "fn", "repo:prod")
            .await
            .unwrap();
        let lines: Vec<&str> = sink.0.iter().map(|(_, _, text)| text.as_str()).collect();
        assert_eq!(
            lines,
            vec![
                "lambda function fn was updated.",
                "  - FunctionName: fn",
                "  - Timeout: 30",
                "  - MemorySize: 512",
                "  - Version: $LATEST",
                "  - Environment: {\"Variables\":{\"A\":\"1\"}}",
            ]
        );
        assert!(sink
            .0
            .iter()
            .all(|(stream, _, _)| *stream == Stream::Stdout));
    }

    #[tokio::test]
    async fn test_update_code_conflict_is_explained() {
        let api = FakeLambda {
            conflict: true,
            ..Default::default()
        };
        let error = update_code(&api, &mut Recorder::default(), "fn", "repo:prod")
            .await
            .unwrap_err();
        assert_eq!(
            error.to_string(),
            "Lambda function 'fn' が別の更新処理中のため、code を更新できませんでした。\
             同じ stage の `pocket deploy` を並行実行していないか確認し、\
             実行中ならその完了を待ってから再実行してください (deploy は数分かかります)。"
        );
    }

    #[tokio::test]
    async fn test_wait_update_until_successful() {
        let api = FakeLambda::default().with_states(
            "fn",
            &[Some("InProgress"), Some("InProgress"), Some("Successful")],
        );
        wait_update(&api, "fn", quick(5)).await.unwrap();
    }

    #[tokio::test]
    async fn test_wait_update_fails_on_failed_status() {
        let api = FakeLambda::default().with_states("fn", &[Some("InProgress"), Some("Failed")]);
        let error = wait_update(&api, "fn", quick(5)).await.unwrap_err();
        assert_eq!(
            error.to_string(),
            "lambda function fn の更新が失敗しました (LastUpdateStatus=Failed)。\
             image URI や設定を確認してください。"
        );
    }

    #[tokio::test]
    async fn test_wait_update_gives_up() {
        let api = FakeLambda::default().with_states("fn", &[Some("InProgress")]);
        let error = wait_update(&api, "fn", quick(3)).await.unwrap_err();
        assert_eq!(
            error.to_string(),
            "Lambda couldn't stop updating. Please check."
        );
    }

    const TOML: &str = r#"
[general]
region = "ap-southeast-1"
project_name = "testprj"
stages = ["prod"]

[container.main]
dockerfile_path = "Dockerfile"
envs = { DEPLOY_HASH = "new" }

[container.main.handlers.wsgi]
command = "a.b"

[container.main.handlers.worker]
command = "a.c"

[container.main.handlers.missing]
command = "a.d"
"#;

    #[tokio::test]
    async fn test_sync_deploy_hash_updates_only_stale_functions() {
        let ctx = context_from_toml(TOML, "prod");
        let c = ctx.container.get("main").unwrap();
        let name = |key: &str| c.handlers.get(key).unwrap().function_name.clone();
        let api = FakeLambda::default()
            .with_states(&name("wsgi"), &[Some("Successful")])
            .with_states(&name("worker"), &[Some("Successful")])
            .with_env(&name("wsgi"), &[("DEPLOY_HASH", "old"), ("KEEP", "1")])
            .with_env(&name("worker"), &[("DEPLOY_HASH", "new")]);
        let mut sink = Recorder::default();
        sync_deploy_hash(&api, &mut sink, c, quick(3))
            .await
            .unwrap();
        // 古い function だけを、他の環境変数を保ったまま更新する。無い function は飛ばす
        assert_eq!(
            *api.calls.borrow(),
            vec![format!(
                "update_environment {} DEPLOY_HASH=new,KEEP=1",
                name("wsgi")
            )]
        );
        let logs: Vec<&str> = sink
            .0
            .iter()
            .filter(|(_, style, _)| *style == Style::Log)
            .map(|(_, _, text)| text.as_str())
            .collect();
        assert_eq!(
            logs,
            vec!["[wsgi] Lambda env DEPLOY_HASH を同期します (old → new)"]
        );
    }

    #[tokio::test]
    async fn test_sync_deploy_hash_reports_none_for_unset_value() {
        let ctx = context_from_toml(TOML, "prod");
        let c = ctx.container.get("main").unwrap();
        let name = c.handlers.get("wsgi").unwrap().function_name.clone();
        let api = FakeLambda::default().with_states(&name, &[Some("Successful")]);
        let mut sink = Recorder::default();
        sync_deploy_hash(&api, &mut sink, c, quick(3))
            .await
            .unwrap();
        assert!(sink
            .0
            .iter()
            .any(|(_, _, text)| text == "[wsgi] Lambda env DEPLOY_HASH を同期します (None → new)"));
    }
}
