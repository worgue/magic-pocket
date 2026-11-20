//! `pocket-rs django init` / `manage` / `resetdb` (Python 版 `pocket_cli/django_cli.py`)。
//!
//! ローカルの Django を読み込まないコマンドだけを持つ (Rust 版は Python を import しない)。
//! `manage` / `resetdb` は Python 版と同じく management command の handler を非同期
//! (InvocationType=Event) で invoke し、CloudWatch Logs を追って、handler の成功の印
//! (`POCKET_MANAGE_HANDLER_SUCCESS`) が REPORT 行までに出たかで成否を決める。
//!
//! Python 版との違い:
//! - `--stage` を省略したら入力を求めずエラー (env `POCKET_DEPLOY_STAGE` は読む)
//! - `manage` は django が入っていなくても動く (Python 版は django の import を確かめる)
//! - `init` の django-environ の有無は、project の Python (uv があれば `uv run python`、無ければ
//!   `python3` / `python`) に問い合わせる。Python 版は pocket 自身の Python で確かめる
//! - INIT の失敗の説明に、同じ process の build が検出した mode 600 のファイルを足さない
//!   (`manage` は build をしないので、Python 版でも足されない)

use std::collections::BTreeMap;
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{bail, Context as _, Result};
use aws_config::SdkConfig;
use aws_sdk_lambda::primitives::{DateTime, DateTimeFormat};
use clap::Subcommand;
use magic_pocket_settings::context::{Context, LambdaHandlerContext};
use magic_pocket_settings::pyjson;
use minijinja::Environment;
use serde_json::json;

use crate::aws_util::{has_code, load_sdk, regional_config, sdk_error};
use crate::context::load_context_read_only;
use crate::deploy::generate::django_secret_key;
use crate::deploy::interaction::{Interaction, Prompt};
use crate::resource::dsql_backup::py_iso;
use crate::status::sink::{Sink, Terminal};

/// management command handler の command (pocket.toml の handlers.*.command)
const MANAGEMENT_COMMAND: &str = "pocket.django.lambda_handlers.management_command_handler";

/// handler が例外なく完了したときだけ出す印 (Python の `MANAGE_HANDLER_SUCCESS_SENTINEL`)
const SUCCESS_SENTINEL: &str = "POCKET_MANAGE_HANDLER_SUCCESS";

/// ログを待つ既定の秒数 (Python の `show_logs` の `timeout_seconds`)
const DEFAULT_TIMEOUT_SECONDS: u64 = 120;

/// `django init` が書くファイルの template (Python 版と同じファイル)
const POCKET_TOML_TEMPLATE: &str =
    include_str!("../../../packages/magic-pocket-cli/pocket_cli/templates/init/pocket_simple.toml");
const DOCKERFILE_TEMPLATE: &str =
    include_str!("../../../packages/magic-pocket-cli/pocket_cli/templates/init/pocket.Dockerfile");
const SETTINGS_TEMPLATE: &str =
    include_str!("../../../packages/magic-pocket-cli/pocket_cli/templates/init/django-settings.py");
const DOTENV_TEMPLATE: &str =
    include_str!("../../../packages/magic-pocket-cli/pocket_cli/templates/init/django-dotenv.env");

#[derive(Subcommand)]
pub enum DjangoCommand {
    /// pocket.toml・pocket.Dockerfile・settings.py・.env の雛形を書く
    Init,
    /// management command を Lambda で実行し、CloudWatch Logs の出力を表示する
    ///
    /// command の後ろの引数はそのまま command に渡す (`--stage` と `--timeout-seconds` は
    /// command の後ろに書いてもこのコマンドの option として読む。`--` より後ろは読まない)
    Manage {
        /// stage 名
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: Option<String>,
        /// ログを待つ秒数 (既定 120)
        #[arg(long)]
        timeout_seconds: Option<String>,
        /// management command と引数
        #[arg(
            required = true,
            trailing_var_arg = true,
            allow_hyphen_values = true,
            value_name = "COMMAND [ARGS]..."
        )]
        words: Vec<String>,
    },
    /// データベースの public スキーマをリセットする
    ///
    /// Lambda 経由で DROP SCHEMA public CASCADE; CREATE SCHEMA public; を実行し、全テーブルを
    /// 削除してマイグレーションをやり直せる状態にする
    Resetdb {
        /// stage 名
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        /// 確認プロンプトをスキップ
        #[arg(long, short = 'y')]
        yes: bool,
    },
}

pub async fn run(command: DjangoCommand) -> Result<()> {
    let mut sink = Terminal::new();
    match command {
        DjangoCommand::Init => {
            let dir = std::env::current_dir()?;
            let project_name = dir
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default();
            let environ = environ_installed();
            let prompt = Interaction { assume_yes: false };
            init(
                &dir,
                &project_name,
                environ,
                &django_secret_key()?,
                &prompt,
                &mut sink,
            )
        }
        DjangoCommand::Manage {
            stage,
            timeout_seconds,
            words,
        } => {
            let args = ManageArgs::parse(stage, timeout_seconds, words)?;
            let (context, _) = load_context_read_only(&args.stage)?;
            let handler = management_command_handler(&context, &mut sink)?;
            let api = AwsManage::new(&load_sdk().await, &handler.region);
            let payload = payload(&args.command, &args.args);
            manage(&api, handler, &payload, args.timeout_seconds, &mut sink).await
        }
        DjangoCommand::Resetdb { stage, yes } => {
            resetdb_confirm(&stage, &Interaction { assume_yes: yes }, &mut sink)?;
            let (context, _) = load_context_read_only(&stage)?;
            let handler = management_command_handler(&context, &mut sink)?;
            let api = AwsManage::new(&load_sdk().await, &handler.region);
            let payload = payload("pocket_resetdb", &[]);
            manage(&api, handler, &payload, DEFAULT_TIMEOUT_SECONDS, &mut sink).await?;
            sink.success("データベースをリセットしました。");
            sink.info(&format!(
                "pocket django manage --stage {stage} migrate を実行してください。"
            ));
            Ok(())
        }
    }
}

// ---- init ----

fn render(template: &str, variables: serde_json::Value) -> Result<String> {
    let mut env = Environment::new();
    // Jinja2 の keep_trailing_newline=True と同じ
    env.set_syntax(
        minijinja::syntax::SyntaxConfig::builder()
            .keep_trailing_newline(true)
            .build()?,
    );
    Ok(env.render_str(template, minijinja::value::Serde(&variables))?)
}

/// Python 版の `importlib.util.find_spec("environ")` に当たる。project の Python に聞く
fn environ_installed() -> bool {
    const CHECK: &str =
        "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('environ') else 1)";
    let Some(mut command) = python_command() else {
        return false;
    };
    command
        .args(["-c", CHECK])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_ok_and(|status| status.success())
}

/// Python の `_build_python_command`: uv があれば `uv run python`、無ければ python3 / python
fn python_command() -> Option<Command> {
    let path = std::env::var_os("PATH")?;
    let find = |name: &str| {
        std::env::split_paths(&path)
            .map(|dir| dir.join(name))
            .find(|candidate| candidate.is_file())
    };
    if let Some(uv) = find("uv") {
        let mut command = Command::new(uv);
        command.args(["run", "python"]);
        return Some(command);
    }
    ["python3", "python"]
        .into_iter()
        .find_map(find)
        .map(Command::new)
}

/// `django init` (Python 版の `init` と `_update_dotenv`)。`dir` は作業ディレクトリ
pub fn init(
    dir: &Path,
    project_name: &str,
    environ_installed: bool,
    secret_key: &str,
    prompt: &impl Prompt,
    sink: &mut dyn Sink,
) -> Result<()> {
    let pocket_toml = dir.join("pocket.toml");
    if confirm_overwrite(&pocket_toml, "pocket.toml", prompt, sink)? {
        std::fs::write(&pocket_toml, render(POCKET_TOML_TEMPLATE, json!({}))?)?;
        sink.success("Update: pocket.toml");
    }
    let dockerfile = dir.join("pocket.Dockerfile");
    if confirm_overwrite(&dockerfile, "pocket.Dockerfile", prompt, sink)? {
        std::fs::write(&dockerfile, render(DOCKERFILE_TEMPLATE, json!({}))?)?;
        sink.success("Update: pocket.Dockerfile");
    }
    if !environ_installed {
        sink.warning("django-environ is not installed");
        sink.warning("`settings.py` and `.env` file will be updated with it.");
        return Ok(());
    }
    let settings_path = dir.join(project_name).join("settings.py");
    if confirm_overwrite(&settings_path, "settings.py", prompt, sink)? {
        // Python 版は開いてから (書く前に) 報告する。開けなければ報告しない
        let mut file = std::fs::File::create(&settings_path)
            .with_context(|| format!("{} を開けません", settings_path.display()))?;
        sink.success("Update: settings.py");
        std::io::Write::write_all(
            &mut file,
            render(SETTINGS_TEMPLATE, json!({ "project_name": project_name }))?.as_bytes(),
        )?;
    }
    update_dotenv(dir, secret_key, prompt, sink)
}

/// 既存のファイルは上書きしてよいか確かめる。無いファイルは聞かずに書く
fn confirm_overwrite(
    path: &Path,
    label: &str,
    prompt: &impl Prompt,
    sink: &mut dyn Sink,
) -> Result<bool> {
    if !path.exists() {
        return Ok(true);
    }
    sink.warning(&format!("{label} already exists"));
    if prompt.confirm(&format!("Do you want to overwrite {label}?"), false)? {
        return Ok(true);
    }
    sink.warning(&format!("Skip: {label}"));
    Ok(false)
}

fn update_dotenv(
    dir: &Path,
    secret_key: &str,
    prompt: &impl Prompt,
    sink: &mut dyn Sink,
) -> Result<()> {
    let dotenv_path = dir.join(".env");
    let content = render(DOTENV_TEMPLATE, json!({ "secret_key": secret_key }))?;
    sink.info("You may need to update .env file");
    if prompt.confirm("Do you want to check the content?", true)? {
        sink.info(&content);
    }
    if !prompt.confirm("Do you want to create .env file?", false)? {
        return Ok(());
    }
    if dotenv_path.exists() {
        sink.warning(".env file already exists");
        if !prompt.confirm("Do you want to overwrite .env file?", false)? {
            sink.warning("ensure you have correct values in .env file");
            sink.info("sample .env file:");
            sink.info(&content);
            return Ok(());
        } else if prompt.confirm("Log the deleting .env?", true)? {
            sink.info(&std::fs::read_to_string(&dotenv_path)?);
            sink.danger("The content above was deleted");
        }
    }
    std::fs::write(&dotenv_path, content)?;
    sink.success("Update: .env");
    Ok(())
}

// ---- manage / resetdb ----

/// `manage` の引数 (click の `ignore_unknown_options` と同じく、command の後ろの
/// `--stage` / `--timeout-seconds` もこのコマンドの option として読む)
#[derive(Debug, PartialEq)]
pub struct ManageArgs {
    pub stage: String,
    pub timeout_seconds: u64,
    pub command: String,
    pub args: Vec<String>,
}

impl ManageArgs {
    pub fn parse(
        stage: Option<String>,
        timeout_seconds: Option<String>,
        words: Vec<String>,
    ) -> Result<Self> {
        let mut stage = stage;
        let mut timeout = timeout_seconds;
        let mut rest: Vec<String> = Vec::new();
        let mut words = words.into_iter();
        while let Some(word) = words.next() {
            if word == "--" {
                rest.extend(words.by_ref());
                break;
            }
            let (target, value) = if let Some(v) = word.strip_prefix("--stage=") {
                (&mut stage, Some(v.to_string()))
            } else if let Some(v) = word.strip_prefix("--timeout-seconds=") {
                (&mut timeout, Some(v.to_string()))
            } else if word == "--stage" {
                (&mut stage, None)
            } else if word == "--timeout-seconds" {
                (&mut timeout, None)
            } else {
                rest.push(word);
                continue;
            };
            let value = match value {
                Some(value) => value,
                None => match words.next() {
                    Some(value) => value,
                    None => bail!("Option '{word}' requires an argument."),
                },
            };
            *target = Some(value);
        }
        let Some(stage) = stage else {
            bail!("Missing option '--stage'.");
        };
        // click の type=int と同じく整数だけを受け付ける。0 は Python 版と同じく既定
        let timeout_seconds = match timeout {
            Some(value) => match value.trim().parse::<u64>() {
                Ok(0) => DEFAULT_TIMEOUT_SECONDS,
                Ok(seconds) => seconds,
                Err(_) => bail!(
                    "Invalid value for '--timeout-seconds': '{value}' is not a valid integer."
                ),
            },
            None => DEFAULT_TIMEOUT_SECONDS,
        };
        let mut rest = rest.into_iter();
        let Some(command) = rest.next() else {
            bail!("Missing argument 'COMMAND'.");
        };
        Ok(Self {
            stage,
            timeout_seconds,
            command,
            args: rest.collect(),
        })
    }
}

/// handler に渡す payload (Python の `json.dumps({"command": …, "args": …})`)
fn payload(command: &str, args: &[String]) -> String {
    pyjson::dumps(&json!({ "command": command, "args": args }))
}

/// Python の `_get_management_command_handler`
fn management_command_handler<'c>(
    context: &'c Context,
    sink: &mut dyn Sink,
) -> Result<&'c LambdaHandlerContext> {
    management_command_handler_with(context, std::env::var("POCKET_CONTAINER").ok(), sink)
}

fn management_command_handler_with<'c>(
    context: &'c Context,
    env_name: Option<String>,
    sink: &mut dyn Sink,
) -> Result<&'c LambdaHandlerContext> {
    if context.container.is_empty() {
        bail!("container is not configured for this stage");
    }
    let containers: BTreeMap<&String, _> = context.container.iter().collect();
    let mut matches: Vec<(&str, &str, &LambdaHandlerContext)> = Vec::new();
    for (container, c_ctx) in containers {
        for (key, handler) in c_ctx.handlers.iter() {
            if handler.command == MANAGEMENT_COMMAND {
                matches.push((container, key, handler));
            }
        }
    }
    if matches.is_empty() {
        sink.print("management command handler not found");
        bail!("Add management command handler for this stage");
    }
    if let (true, Some(env_name)) = (matches.len() > 1, env_name.filter(|n| !n.is_empty())) {
        let filtered: Vec<_> = matches
            .iter()
            .copied()
            .filter(|(container, _, _)| *container == env_name)
            .collect();
        if !filtered.is_empty() {
            matches = filtered;
        }
    }
    if matches.len() > 1 {
        let names: Vec<String> = matches
            .iter()
            .map(|(container, key, _)| format!("{container}.{key}"))
            .collect();
        bail!(
            "management command handler が複数の container にあります ({})。\
             POCKET_CONTAINER 環境変数で対象を指定してください。",
            names.join(", ")
        );
    }
    Ok(matches[0].2)
}

/// `resetdb` の確認 (Context を読む前に聞く。Python 版と同じ順)
fn resetdb_confirm(stage: &str, prompt: &impl Prompt, sink: &mut dyn Sink) -> Result<()> {
    sink.danger(&format!("stage '{stage}' のデータベースをリセットします。"));
    sink.danger("全テーブルとデータが削除されます。");
    if !prompt.confirm("本当に実行しますか？", false)? {
        bail!("Aborted!");
    }
    Ok(())
}

/// CloudWatch Logs の 1 件
#[derive(Debug, Clone, PartialEq)]
pub struct LogEvent {
    pub stream: String,
    pub timestamp: i64,
    pub message: String,
}

/// 非同期 invoke の応答
#[derive(Debug, Clone, PartialEq)]
pub struct Invoked {
    pub request_id: String,
    /// 応答の Date header
    pub created_at: DateTime,
}

/// `manage` が呼ぶ AWS の API と待ち
pub trait ManageApi {
    /// InvocationType=Event で invoke する
    async fn invoke_event(&self, function_name: &str, payload: &str) -> Result<Invoked>;
    /// 最後のイベントが新しい順に `limit` 本の log stream (log group が無ければ空)
    async fn recent_log_streams(&self, log_group: &str, limit: i32) -> Result<Vec<String>>;
    /// FilterLogEvents の 1 ページ
    async fn filter_log_events(
        &self,
        log_group: &str,
        streams: &[String],
        pattern: Option<&str>,
        start_time: i64,
        next_token: Option<String>,
    ) -> Result<(Vec<LogEvent>, Option<String>)>;
    async fn sleep(&self, duration: Duration);
}

/// Python の `LambdaHandler.invoke` + `show_logs`
pub async fn manage(
    api: &impl ManageApi,
    handler: &LambdaHandlerContext,
    payload: &str,
    timeout_seconds: u64,
    sink: &mut dyn Sink,
) -> Result<()> {
    let invoked = api.invoke_event(&handler.function_name, payload).await?;
    sink.print(&format!("lambda request_id: {}", invoked.request_id));
    sink.print(&format!(
        "lambda created_at: {}",
        py_iso(invoked.created_at).replacen('T', " ", 1)
    ));
    let start_pattern = format!("\"START RequestId: {}\"", invoked.request_id);
    let report_prefix = format!("REPORT RequestId: {}", invoked.request_id);
    let created_at_ms = invoked.created_at.secs() * 1000;
    let first = find_first_event(api, handler, &start_pattern, created_at_ms, sink).await?;
    sink.print(&format!("Log stream found: {}", first.stream));
    let mut printed: Vec<String> = Vec::new();
    let mut success_seen = false;
    const SLEEP_SECONDS: u64 = 5;
    for _ in 0..timeout_seconds / SLEEP_SECONDS {
        let messages = all_messages(api, handler, &first.stream, first.timestamp).await?;
        if messages.len() < printed.len() || messages[..printed.len()] != printed[..] {
            bail!("log stream changed");
        }
        for message in messages.into_iter().skip(printed.len()) {
            sink.print(message.trim());
            if message.contains(SUCCESS_SENTINEL) {
                success_seen = true;
            }
            let is_report = message.starts_with(&report_prefix);
            printed.push(message);
            if is_report {
                if success_seen {
                    return Ok(());
                }
                if looks_like_init_failure(&printed.join("\n")) {
                    bail!("{}", init_failure_message());
                }
                bail!(
                    "management command handler did not complete successfully (no success \
                     marker before REPORT). 上の CloudWatch ログの traceback を確認してください。"
                );
            }
        }
        api.sleep(Duration::from_secs(SLEEP_SECONDS)).await;
    }
    bail!(
        "Timeout {timeout_seconds} seconds: 成功マーカーを観測できませんでした。CloudWatch の\
         ログを確認してください。処理が長い場合は --timeout-seconds を増やしてください。"
    )
}

/// Python の `_find_events`: START 行を含む最初のイベント
async fn find_first_event(
    api: &impl ManageApi,
    handler: &LambdaHandlerContext,
    pattern: &str,
    start_time: i64,
    sink: &mut dyn Sink,
) -> Result<LogEvent> {
    let mut streams: Vec<String> = Vec::new();
    for i in 0..20 {
        if i != 0 {
            sink.print_inline(if i == 1 {
                "Waiting for log stream."
            } else {
                "."
            });
            api.sleep(Duration::from_secs(3)).await;
        }
        streams = api.recent_log_streams(&handler.log_group_name, 3).await?;
        if streams.is_empty() {
            continue;
        }
        let mut events: Vec<LogEvent> = Vec::new();
        let mut token = None;
        for _ in 0..10 {
            let (page, next) = api
                .filter_log_events(
                    &handler.log_group_name,
                    &streams,
                    Some(pattern),
                    start_time,
                    token,
                )
                .await?;
            events.extend(page);
            token = next;
            if token.is_none() {
                break;
            }
        }
        if let Some(first) = events.into_iter().next() {
            return Ok(first);
        }
    }
    sink.print(&format!("Searched {} log stream below...", streams.len()));
    for stream in &streams {
        sink.print(&format!("  - {stream}"));
    }
    bail!("log stream not found")
}

/// Python の `_filter_log_messages`: nextToken を辿って全件
async fn all_messages(
    api: &impl ManageApi,
    handler: &LambdaHandlerContext,
    stream: &str,
    start_time: i64,
) -> Result<Vec<String>> {
    let streams = [stream.to_string()];
    let mut messages = Vec::new();
    let mut token = None;
    loop {
        let (events, next) = api
            .filter_log_events(&handler.log_group_name, &streams, None, start_time, token)
            .await?;
        messages.extend(events.into_iter().map(|e| e.message));
        match next {
            Some(next) => token = Some(next),
            None => return Ok(messages),
        }
    }
}

/// Python の `_looks_like_init_failure`
fn looks_like_init_failure(log_text: &str) -> bool {
    log_text.contains("Runtime.Unknown")
        || log_text
            .lines()
            .any(|line| line.contains("INIT_REPORT") && line.contains("Status: error"))
}

fn init_failure_message() -> String {
    concat!(
        "Lambda が INIT フェーズで失敗しました ",
        "(Runtime.Unknown / INIT_REPORT ... Status: error)。",
        "これはアプリの例外ではなく runtime 側の問題の可能性が",
        "高いです。原因候補: ",
        "(1) magic-pocket runtime の版不整合 — pocket.toml で",
        "新しめの機能を使っている場合、runtime が古い可能性が",
        "あります: uv add 'magic-pocket[django]>=",
        "<CLI と同じ版>'。",
        "(2) build context のファイル permission — mode 600 等",
        " (other-read なし) のファイルが image に COPY される",
        "と、Lambda の非 root 実行ユーザーが読めず INIT で ",
        "PermissionError になります。ログに PermissionError が",
        "あれば該当ファイルを chmod 644 して再 deploy して",
        "ください。",
        "アプリの traceback ではなく上の INIT_REPORT / ",
        "Runtime.Unknown 行を確認してください。",
    )
    .to_string()
}

// ---- AWS ----

pub struct AwsManage {
    lambda: aws_sdk_lambda::Client,
    logs: aws_sdk_cloudwatchlogs::Client,
}

impl AwsManage {
    pub fn new(sdk: &SdkConfig, region: &str) -> Self {
        let config = regional_config(sdk, region);
        Self {
            lambda: aws_sdk_lambda::Client::new(&config),
            logs: aws_sdk_cloudwatchlogs::Client::new(&config),
        }
    }
}

/// 応答の Date header を記録する (log を探す起点。Python 版も応答の Date を使う)
#[derive(Debug, Clone, Default)]
struct CaptureDate(Arc<Mutex<Option<String>>>);

impl aws_sdk_lambda::config::Intercept for CaptureDate {
    fn name(&self) -> &'static str {
        "CaptureDate"
    }

    fn read_after_transmit(
        &self,
        context: &aws_sdk_lambda::config::interceptors::BeforeDeserializationInterceptorContextRef<
            '_,
        >,
        _runtime_components: &aws_sdk_lambda::config::RuntimeComponents,
        _cfg: &mut aws_sdk_lambda::config::ConfigBag,
    ) -> Result<(), aws_sdk_lambda::error::BoxError> {
        if let Some(date) = context.response().headers().get("date") {
            if let Ok(mut slot) = self.0.lock() {
                *slot = Some(date.to_string());
            }
        }
        Ok(())
    }
}

impl ManageApi for AwsManage {
    async fn invoke_event(&self, function_name: &str, payload: &str) -> Result<Invoked> {
        use aws_sdk_lambda::operation::RequestId as _;
        let capture = CaptureDate::default();
        let response = self
            .lambda
            .invoke()
            .function_name(function_name)
            .invocation_type(aws_sdk_lambda::types::InvocationType::Event)
            .payload(aws_sdk_lambda::primitives::Blob::new(payload.as_bytes()))
            .customize()
            .interceptor(capture.clone())
            .send()
            .await
            .map_err(|e| sdk_error("lambda invoke", &e))?;
        let request_id = response
            .request_id()
            .context("invoke の応答に request id がありません")?
            .to_string();
        let date = capture
            .0
            .lock()
            .ok()
            .and_then(|slot| slot.clone())
            .context("invoke の応答に Date header がありません")?;
        let created_at = DateTime::from_str(&date, DateTimeFormat::HttpDate)
            .with_context(|| format!("invoke の応答の Date header を読めません: {date}"))?;
        Ok(Invoked {
            request_id,
            created_at,
        })
    }

    async fn recent_log_streams(&self, log_group: &str, limit: i32) -> Result<Vec<String>> {
        let response = match self
            .logs
            .describe_log_streams()
            .log_group_name(log_group)
            .order_by(aws_sdk_cloudwatchlogs::types::OrderBy::LastEventTime)
            .descending(true)
            .limit(limit)
            .send()
            .await
        {
            Ok(response) => response,
            Err(e) if has_code(&e, &["ResourceNotFoundException"]) => return Ok(Vec::new()),
            Err(e) => return Err(sdk_error("logs describe_log_streams", &e)),
        };
        Ok(response
            .log_streams()
            .iter()
            .filter_map(|s| s.log_stream_name().map(str::to_string))
            .collect())
    }

    async fn filter_log_events(
        &self,
        log_group: &str,
        streams: &[String],
        pattern: Option<&str>,
        start_time: i64,
        next_token: Option<String>,
    ) -> Result<(Vec<LogEvent>, Option<String>)> {
        let response = self
            .logs
            .filter_log_events()
            .log_group_name(log_group)
            .set_log_stream_names(Some(streams.to_vec()))
            .set_filter_pattern(pattern.map(str::to_string))
            .start_time(start_time)
            .set_next_token(next_token)
            .send()
            .await
            .map_err(|e| sdk_error("logs filter_log_events", &e))?;
        let events = response
            .events()
            .iter()
            .map(|e| LogEvent {
                stream: e.log_stream_name().unwrap_or_default().to_string(),
                timestamp: e.timestamp().unwrap_or_default(),
                message: e.message().unwrap_or_default().to_string(),
            })
            .collect();
        Ok((events, response.next_token().map(str::to_string)))
    }

    async fn sleep(&self, duration: Duration) {
        tokio::time::sleep(duration).await;
    }
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;
    use std::collections::VecDeque;

    use clap::Parser;

    use super::*;
    use crate::stack::testing::context_from_toml;
    use crate::status::sink::{Recorded, Stream, Style};

    /// 聞かれた順に決めた答えを返す
    struct Scripted {
        answers: RefCell<VecDeque<bool>>,
        asked: RefCell<Vec<(String, bool)>>,
    }

    impl Scripted {
        fn new(answers: &[bool]) -> Self {
            Self {
                answers: RefCell::new(answers.iter().copied().collect()),
                asked: RefCell::default(),
            }
        }
    }

    impl Prompt for Scripted {
        fn confirm(&self, text: &str, default: bool) -> Result<bool> {
            self.asked.borrow_mut().push((text.to_string(), default));
            self.answers
                .borrow_mut()
                .pop_front()
                .context("想定より多く聞かれた")
        }
        fn confirm_explicit(&self, text: &str) -> Result<bool> {
            self.confirm(text, false)
        }
        fn asks_explicitly(&self) -> bool {
            true
        }
    }

    fn texts(sink: &Recorded) -> Vec<(Style, &str)> {
        sink.0
            .iter()
            .map(|(_, style, t)| (*style, t.as_str()))
            .collect()
    }

    const KEY: &str = "k3y";

    fn init_dir(dotenv: Option<&str>) -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir(dir.path().join("myproj")).unwrap();
        if let Some(text) = dotenv {
            std::fs::write(dir.path().join(".env"), text).unwrap();
        }
        dir
    }

    #[test]
    fn test_init_writes_templates_and_new_dotenv() {
        let dir = init_dir(None);
        let prompt = Scripted::new(&[false, true]);
        let mut sink = Recorded::default();
        init(dir.path(), "myproj", true, KEY, &prompt, &mut sink).unwrap();
        let read = |p: &str| std::fs::read_to_string(dir.path().join(p)).unwrap();
        assert_eq!(read("pocket.toml"), POCKET_TOML_TEMPLATE);
        assert_eq!(read("pocket.Dockerfile"), DOCKERFILE_TEMPLATE);
        assert_eq!(
            read("myproj/settings.py"),
            SETTINGS_TEMPLATE.replace("{{ project_name }}", "myproj")
        );
        assert_eq!(
            read(".env"),
            "DEBUG=true\nSECRET_KEY=k3y\nDATABASE_URL=sqlite:///db.sqlite3\n"
        );
        assert_eq!(
            *prompt.asked.borrow(),
            vec![
                ("Do you want to check the content?".to_string(), true),
                ("Do you want to create .env file?".to_string(), false),
            ]
        );
        assert_eq!(
            texts(&sink),
            vec![
                (Style::Success, "Update: pocket.toml"),
                (Style::Success, "Update: pocket.Dockerfile"),
                (Style::Success, "Update: settings.py"),
                (Style::Info, "You may need to update .env file"),
                (Style::Success, "Update: .env"),
            ]
        );
    }

    #[test]
    fn test_init_existing_dotenv_overwrite_and_keep() {
        // 上書きする (消す内容も表示する)
        let dir = init_dir(Some("OLD=1\n"));
        let prompt = Scripted::new(&[true, true, true, true]);
        let mut sink = Recorded::default();
        init(dir.path(), "myproj", true, KEY, &prompt, &mut sink).unwrap();
        let dotenv = std::fs::read_to_string(dir.path().join(".env")).unwrap();
        assert!(dotenv.contains("SECRET_KEY=k3y\n"));
        let content = "DEBUG=true\nSECRET_KEY=k3y\nDATABASE_URL=sqlite:///db.sqlite3\n";
        assert_eq!(
            texts(&sink)[3..],
            [
                (Style::Info, "You may need to update .env file"),
                (Style::Info, content),
                (Style::Warning, ".env file already exists"),
                (Style::Info, "OLD=1\n"),
                (Style::Danger, "The content above was deleted"),
                (Style::Success, "Update: .env"),
            ]
        );

        // 上書きしない (既存の .env は変えずに見本を出す)
        let dir = init_dir(Some("OLD=1\n"));
        let prompt = Scripted::new(&[false, true, false]);
        let mut sink = Recorded::default();
        init(dir.path(), "myproj", true, KEY, &prompt, &mut sink).unwrap();
        assert_eq!(
            std::fs::read_to_string(dir.path().join(".env")).unwrap(),
            "OLD=1\n"
        );
        assert_eq!(
            texts(&sink)[3..],
            [
                (Style::Info, "You may need to update .env file"),
                (Style::Warning, ".env file already exists"),
                (
                    Style::Warning,
                    "ensure you have correct values in .env file"
                ),
                (Style::Info, "sample .env file:"),
                (Style::Info, content),
            ]
        );
    }

    #[test]
    fn test_init_confirms_before_overwriting_existing_files() {
        let dir = init_dir(None);
        for name in ["pocket.toml", "pocket.Dockerfile", "myproj/settings.py"] {
            std::fs::write(dir.path().join(name), "KEEP\n").unwrap();
        }
        // pocket.toml は残す、pocket.Dockerfile は上書き、settings.py は残す、.env は作らない
        let prompt = Scripted::new(&[false, true, false, false, false]);
        let mut sink = Recorded::default();
        init(dir.path(), "myproj", true, KEY, &prompt, &mut sink).unwrap();
        let read = |p: &str| std::fs::read_to_string(dir.path().join(p)).unwrap();
        assert_eq!(read("pocket.toml"), "KEEP\n");
        assert_eq!(read("pocket.Dockerfile"), DOCKERFILE_TEMPLATE);
        assert_eq!(read("myproj/settings.py"), "KEEP\n");
        assert_eq!(
            prompt.asked.borrow()[..3],
            [
                ("Do you want to overwrite pocket.toml?".to_string(), false),
                (
                    "Do you want to overwrite pocket.Dockerfile?".to_string(),
                    false
                ),
                ("Do you want to overwrite settings.py?".to_string(), false),
            ]
        );
        assert_eq!(
            texts(&sink)[..7],
            [
                (Style::Warning, "pocket.toml already exists"),
                (Style::Warning, "Skip: pocket.toml"),
                (Style::Warning, "pocket.Dockerfile already exists"),
                (Style::Success, "Update: pocket.Dockerfile"),
                (Style::Warning, "settings.py already exists"),
                (Style::Warning, "Skip: settings.py"),
                (Style::Info, "You may need to update .env file"),
            ]
        );
    }

    #[test]
    fn test_init_without_environ_skips_settings_and_dotenv() {
        let dir = init_dir(None);
        let prompt = Scripted::new(&[]);
        let mut sink = Recorded::default();
        init(dir.path(), "myproj", false, KEY, &prompt, &mut sink).unwrap();
        assert!(!dir.path().join("myproj/settings.py").exists());
        assert!(!dir.path().join(".env").exists());
        assert_eq!(
            texts(&sink)[2..],
            [
                (Style::Warning, "django-environ is not installed"),
                (
                    Style::Warning,
                    "`settings.py` and `.env` file will be updated with it."
                ),
            ]
        );
    }

    #[test]
    fn test_secret_key_shape() {
        let key = django_secret_key().unwrap();
        assert_eq!(key.chars().count(), 50);
        assert!(key
            .chars()
            .all(|c| "abcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*(-_=+)".contains(c)));
    }

    #[derive(Parser)]
    struct TestCli {
        #[command(subcommand)]
        command: DjangoCommand,
    }

    fn parse_manage(argv: &[&str]) -> Result<ManageArgs> {
        let cli = TestCli::try_parse_from(["pocket-rs", "manage"].iter().chain(argv.iter()))?;
        let DjangoCommand::Manage {
            stage,
            timeout_seconds,
            words,
        } = cli.command
        else {
            unreachable!()
        };
        ManageArgs::parse(stage, timeout_seconds, words)
    }

    fn manage_args(stage: &str, timeout: u64, command: &str, args: &[&str]) -> ManageArgs {
        ManageArgs {
            stage: stage.into(),
            timeout_seconds: timeout,
            command: command.into(),
            args: args.iter().map(|a| a.to_string()).collect(),
        }
    }

    #[test]
    fn test_manage_args_like_click() {
        // option は command の前でも後でもよい。知らない option は command に渡す
        assert_eq!(
            parse_manage(&["--stage", "dev", "migrate"]).unwrap(),
            manage_args("dev", 120, "migrate", &[])
        );
        assert_eq!(
            parse_manage(&[
                "createsuperuser",
                "--username=admin",
                "--noinput",
                "--stage=dev",
                "--timeout-seconds",
                "30"
            ])
            .unwrap(),
            manage_args(
                "dev",
                30,
                "createsuperuser",
                &["--username=admin", "--noinput"]
            )
        );
        assert_eq!(
            parse_manage(&[
                "--timeout-seconds=0",
                "--stage=dev",
                "shell",
                "-c",
                "print(1)"
            ])
            .unwrap(),
            manage_args("dev", 120, "shell", &["-c", "print(1)"])
        );
        // `--` より後ろは読まない
        assert_eq!(
            parse_manage(&["--stage=dev", "shell", "--", "--stage=x"]).unwrap(),
            manage_args("dev", 120, "shell", &["--stage=x"])
        );
        assert_eq!(
            // (env の POCKET_DEPLOY_STAGE に左右されないよう clap を通さない)
            ManageArgs::parse(None, None, vec!["migrate".into()])
                .unwrap_err()
                .to_string(),
            "Missing option '--stage'."
        );
        assert_eq!(
            parse_manage(&["--stage=dev", "--timeout-seconds", "x", "migrate"])
                .unwrap_err()
                .to_string(),
            "Invalid value for '--timeout-seconds': 'x' is not a valid integer."
        );
        assert!(parse_manage(&[]).is_err());
    }

    #[test]
    fn test_payload_matches_python_json_dumps() {
        assert_eq!(
            payload("migrate", &["--fake".into(), "app".into()]),
            r#"{"command": "migrate", "args": ["--fake", "app"]}"#
        );
        assert_eq!(
            payload("pocket_resetdb", &[]),
            r#"{"command": "pocket_resetdb", "args": []}"#
        );
    }

    const TOML: &str = r#"
[general]
region = "ap-southeast-1"
stages = ["dev"]
project_name = "testprj"

[container.web]
dockerfile_path = "Dockerfile"
[container.web.handlers.wsgi]
command = "pocket.django.lambda_handlers.wsgi_handler"
[container.web.handlers.management]
command = "pocket.django.lambda_handlers.management_command_handler"

[container.batch]
dockerfile_path = "Dockerfile"
[container.batch.handlers.manage]
command = "pocket.django.lambda_handlers.management_command_handler"
"#;

    #[test]
    fn test_management_command_handler_resolution() {
        let ctx = context_from_toml(TOML, "dev");
        let mut sink = Recorded::default();
        let error = management_command_handler_with(&ctx, None, &mut sink).unwrap_err();
        assert_eq!(
            error.to_string(),
            "management command handler が複数の container にあります (batch.manage, \
             web.management)。POCKET_CONTAINER 環境変数で対象を指定してください。"
        );
        let handler = management_command_handler_with(&ctx, Some("web".into()), &mut sink).unwrap();
        assert_eq!(handler.key, "management");
        // 当たらない POCKET_CONTAINER は絞り込まない
        assert!(management_command_handler_with(&ctx, Some("x".into()), &mut sink).is_err());

        let single = TOML.split("[container.batch]").next().unwrap();
        let ctx = context_from_toml(single, "dev");
        let handler = management_command_handler_with(&ctx, None, &mut sink).unwrap();
        assert_eq!(handler.key, "management");

        let none = single
            .split("[container.web.handlers.management]")
            .next()
            .unwrap();
        let ctx = context_from_toml(none, "dev");
        let mut sink = Recorded::default();
        let error = management_command_handler_with(&ctx, None, &mut sink).unwrap_err();
        assert_eq!(
            error.to_string(),
            "Add management command handler for this stage"
        );
        assert_eq!(
            sink.0,
            vec![(
                Stream::Stdout,
                Style::Plain,
                "management command handler not found".to_string()
            )]
        );
    }

    /// log stream の出方を順に返す fake
    struct FakeLogs {
        /// recent_log_streams の応答 (呼ぶたびに 1 つ進む。最後は居残る)
        streams: RefCell<VecDeque<Vec<String>>>,
        /// START を探す filter の応答 (同上)
        start: RefCell<VecDeque<Vec<LogEvent>>>,
        /// stream 全体の読み取りの応答 (同上)。各要素はページの列
        tail: RefCell<VecDeque<Vec<Vec<&'static str>>>>,
        calls: RefCell<Vec<String>>,
    }

    fn pop<T: Clone>(queue: &RefCell<VecDeque<T>>) -> T {
        let mut queue = queue.borrow_mut();
        if queue.len() > 1 {
            queue.pop_front().unwrap()
        } else {
            queue.front().cloned().unwrap()
        }
    }

    impl FakeLogs {
        fn new(tail: Vec<Vec<Vec<&'static str>>>) -> Self {
            Self {
                streams: RefCell::new([vec!["s1".to_string()]].into()),
                start: RefCell::new(
                    [vec![LogEvent {
                        stream: "s1".into(),
                        timestamp: 1_700_000_000_500,
                        message: "START RequestId: req-1 Version: $LATEST".into(),
                    }]]
                    .into(),
                ),
                tail: RefCell::new(tail.into()),
                calls: RefCell::default(),
            }
        }
    }

    impl ManageApi for FakeLogs {
        async fn invoke_event(&self, function_name: &str, payload: &str) -> Result<Invoked> {
            self.calls
                .borrow_mut()
                .push(format!("invoke {function_name} {payload}"));
            Ok(Invoked {
                request_id: "req-1".into(),
                created_at: DateTime::from_secs(1_700_000_000),
            })
        }

        async fn recent_log_streams(&self, log_group: &str, limit: i32) -> Result<Vec<String>> {
            self.calls
                .borrow_mut()
                .push(format!("streams {log_group} {limit}"));
            Ok(pop(&self.streams))
        }

        async fn filter_log_events(
            &self,
            _log_group: &str,
            streams: &[String],
            pattern: Option<&str>,
            start_time: i64,
            next_token: Option<String>,
        ) -> Result<(Vec<LogEvent>, Option<String>)> {
            self.calls.borrow_mut().push(format!(
                "filter {streams:?} {pattern:?} {start_time} {next_token:?}"
            ));
            if pattern.is_some() {
                return Ok((pop(&self.start), None));
            }
            // tail は 1 回の読み取りでページを辿る。ページの番号は next_token に入れる
            let page: usize = next_token.map(|t| t.parse().unwrap()).unwrap_or(0);
            let pages = {
                let tail = self.tail.borrow();
                tail.front().cloned().unwrap()
            };
            let events = pages[page]
                .iter()
                .map(|m| LogEvent {
                    stream: "s1".into(),
                    timestamp: 0,
                    message: m.to_string(),
                })
                .collect();
            if page + 1 < pages.len() {
                Ok((events, Some((page + 1).to_string())))
            } else {
                if self.tail.borrow().len() > 1 {
                    self.tail.borrow_mut().pop_front();
                }
                Ok((events, None))
            }
        }

        async fn sleep(&self, duration: Duration) {
            self.calls
                .borrow_mut()
                .push(format!("sleep {}", duration.as_secs()));
        }
    }

    fn handler() -> LambdaHandlerContext {
        let ctx = context_from_toml(TOML.split("[container.batch]").next().unwrap(), "dev");
        ctx.container
            .get("web")
            .unwrap()
            .handlers
            .get("management")
            .unwrap()
            .clone()
    }

    fn stdout(sink: &Recorded) -> Vec<String> {
        sink.0
            .iter()
            .map(|(stream, style, text)| {
                assert_eq!(*stream, Stream::Stdout);
                match style {
                    Style::Inline => format!("<{text}>"),
                    _ => text.clone(),
                }
            })
            .collect()
    }

    #[tokio::test]
    async fn test_manage_prints_logs_until_report_with_sentinel() {
        let api = FakeLogs::new(vec![
            vec![vec![
                "START RequestId: req-1 Version: $LATEST\n",
                "Operations:\n",
            ]],
            vec![
                vec!["START RequestId: req-1 Version: $LATEST\n", "Operations:\n"],
                vec![
                    "  Apply all migrations\n",
                    "POCKET_MANAGE_HANDLER_SUCCESS\n",
                    "END RequestId: req-1\n",
                    "REPORT RequestId: req-1\tDuration: 1 ms\n",
                ],
            ],
        ]);
        let handler = handler();
        let mut sink = Recorded::default();
        manage(&api, &handler, "{}", 120, &mut sink).await.unwrap();
        assert_eq!(
            stdout(&sink),
            vec![
                "lambda request_id: req-1",
                "lambda created_at: 2023-11-14 22:13:20+00:00",
                "Log stream found: s1",
                "START RequestId: req-1 Version: $LATEST",
                "Operations:",
                "Apply all migrations",
                "POCKET_MANAGE_HANDLER_SUCCESS",
                "END RequestId: req-1",
                "REPORT RequestId: req-1\tDuration: 1 ms",
            ]
        );
        let calls = api.calls.borrow();
        assert_eq!(calls[0], format!("invoke {} {{}}", handler.function_name));
        assert_eq!(calls[1], format!("streams {} 3", handler.log_group_name));
        // START は Date header の秒から探し、tail は START の時刻から読む
        assert_eq!(
            calls[2],
            "filter [\"s1\"] Some(\"\\\"START RequestId: req-1\\\"\") 1700000000000 None"
        );
        assert_eq!(calls[3], "filter [\"s1\"] None 1700000000500 None");
        assert_eq!(calls[4], "sleep 5");
    }

    #[tokio::test]
    async fn test_manage_fails_without_sentinel_and_detects_init_failure() {
        let api = FakeLogs::new(vec![vec![vec![
            "START RequestId: req-1\n",
            "Traceback ...\n",
            "REPORT RequestId: req-1\n",
        ]]]);
        let mut sink = Recorded::default();
        let error = manage(&api, &handler(), "{}", 120, &mut sink)
            .await
            .unwrap_err();
        assert!(error
            .to_string()
            .starts_with("management command handler did not complete successfully"));

        let api = FakeLogs::new(vec![vec![vec![
            "INIT_REPORT Init Duration: 1 ms Phase: init Status: error\n",
            "REPORT RequestId: req-1\n",
        ]]]);
        let error = manage(&api, &handler(), "{}", 120, &mut sink)
            .await
            .unwrap_err();
        assert_eq!(error.to_string(), init_failure_message());
        assert!(error
            .to_string()
            .contains("uv add 'magic-pocket[django]>=<CLI と同じ版>'。"));
    }

    #[tokio::test]
    async fn test_manage_timeout_and_changed_stream() {
        let api = FakeLogs::new(vec![vec![vec!["START RequestId: req-1\n"]]]);
        let mut sink = Recorded::default();
        let error = manage(&api, &handler(), "{}", 12, &mut sink)
            .await
            .unwrap_err();
        assert!(error.to_string().starts_with("Timeout 12 seconds: "));
        // 12 // 5 = 2 回読む
        let sleeps = api
            .calls
            .borrow()
            .iter()
            .filter(|c| *c == "sleep 5")
            .count();
        assert_eq!(sleeps, 2);

        let api = FakeLogs::new(vec![
            vec![vec!["START RequestId: req-1\n"]],
            vec![vec!["other\n"]],
        ]);
        let error = manage(&api, &handler(), "{}", 120, &mut sink)
            .await
            .unwrap_err();
        assert_eq!(error.to_string(), "log stream changed");
    }

    #[tokio::test]
    async fn test_manage_waits_for_log_stream() {
        let api = FakeLogs::new(vec![vec![vec![
            "START RequestId: req-1\n",
            "POCKET_MANAGE_HANDLER_SUCCESS\n",
            "REPORT RequestId: req-1\n",
        ]]]);
        *api.streams.borrow_mut() = [vec![], vec![], vec!["s1".to_string()]].into();
        let mut sink = Recorded::default();
        manage(&api, &handler(), "{}", 120, &mut sink)
            .await
            .unwrap();
        assert_eq!(
            stdout(&sink)[2..5],
            [
                "<Waiting for log stream.>".to_string(),
                "<.>".to_string(),
                "Log stream found: s1".to_string(),
            ]
        );

        // 見つからなければ探した stream を出して止める
        let api = FakeLogs::new(vec![vec![vec![]]]);
        *api.start.borrow_mut() = [vec![]].into();
        let mut sink = Recorded::default();
        let error = manage(&api, &handler(), "{}", 120, &mut sink)
            .await
            .unwrap_err();
        assert_eq!(error.to_string(), "log stream not found");
        let out = stdout(&sink);
        assert_eq!(out.iter().filter(|l| *l == "<.>").count(), 18);
        assert_eq!(
            out[out.len() - 2..],
            ["Searched 1 log stream below...", "  - s1"]
        );
    }

    #[test]
    fn test_resetdb_confirm() {
        let mut sink = Recorded::default();
        let error = resetdb_confirm("dev", &Scripted::new(&[false]), &mut sink).unwrap_err();
        assert_eq!(error.to_string(), "Aborted!");
        assert_eq!(
            texts(&sink),
            vec![
                (
                    Style::Danger,
                    "stage 'dev' のデータベースをリセットします。"
                ),
                (Style::Danger, "全テーブルとデータが削除されます。"),
            ]
        );
        let prompt = Interaction { assume_yes: true };
        resetdb_confirm("dev", &prompt, &mut Recorded::default()).unwrap();
    }
}
