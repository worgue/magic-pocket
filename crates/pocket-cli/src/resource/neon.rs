//! `pocket resource neon` (Python 版 `neon_cli.py` / `store_url_helper.py` /
//! `url_helper.py`)。
//!
//! deploy / status は Neon の API を呼ばない。API key を読むのはこのコマンドだけで、
//! 環境変数 `NEON_API_KEY` から読む (`.env` は読まない)。`url` の stdout は URL だけで、
//! 診断はすべて stderr に出す

use anyhow::{bail, Result};
use clap::Subcommand;
use magic_pocket_settings::context::{Context, NeonContext, SecretsContext};
use magic_pocket_settings::mask_secret_values;
use magic_pocket_settings::model::{StoreType, UserSecretSpec, UserSecretType};

use super::neon_api::{ensure_url, DestroyPlan, Neon, NeonApi};
use super::{pyrepr, Stage};
use crate::deploy::interaction::{Interaction, Prompt};
use crate::deploy::secret_store::{AwsSecretStore, SecretStore as _};
use crate::deploy::stored_value::{AwsStoredValues, StoredValues as _};
use crate::status::sink::Sink;

const SECRET_TYPE: &str = "neon_database_url";
const DB_LABEL: &str = "Neon";

#[derive(Subcommand)]
pub enum NeonCommand {
    /// Neon の Context を表示する (secret はマスク)
    Context(Stage),
    /// project・branch・database・endpoint・role の有無を表示する
    Status(Stage),
    /// branch (無ければ)・role・database を作る
    Create(Stage),
    /// database を消して作り直す
    ResetDatabase(Stage),
    /// 別の stage の branch から分岐した branch を作る
    BranchOut {
        #[command(flatten)]
        stage: Stage,
        /// 分岐元の stage
        #[arg(long)]
        base_stage: String,
    },
    /// stage の Neon ブランチを削除する。
    ///
    /// root branch は Neon 仕様で branch 単位の削除ができない (422: cannot delete the
    /// root branch) ため、project 内に他 branch が無ければ再確認のうえ project ごと
    /// 削除する。他 branch が同居している場合は巻き添えになるためエラーで中断する。
    Delete {
        #[command(flatten)]
        stage: Stage,
        /// 確認プロンプトをスキップ
        #[arg(long, short = 'y')]
        yes: bool,
    },
    /// branch/role/db を ensure し DATABASE_URL を stored user secret に保存する。
    ///
    /// deploy は Neon に触れないので、API key がある場所でこのコマンドを実行して
    /// branch / role / database を用意する。Neon の URL は reveal_password 方式で
    /// 冪等なので何度実行しても同じ値。
    StoreUrl {
        #[command(flatten)]
        stage: Stage,
        /// 保存先 user secret のキー (複数候補時に必須)
        #[arg(long)]
        key: Option<String>,
        /// 既存 secret があっても上書きする
        #[arg(long)]
        force: bool,
    },
    /// 接続 URL を stdout に出力する (default: stored-first / --live: provider API)。
    ///
    /// 移行ツール等が `$(pocket resource neon url --stage <s>)` で食える純 URL のみを
    /// stdout に出す (診断は stderr)。
    Url {
        #[command(flatten)]
        stage: Stage,
        /// stored を見ず provider API で live 算出する (Neon は reveal 方式で冪等)
        #[arg(long)]
        live: bool,
    },
}

/// Neon の API key を環境変数から読む (`.env` は読まない)
pub fn neon_api_key() -> Result<String> {
    match std::env::var("NEON_API_KEY") {
        Ok(key) if !key.is_empty() => Ok(key),
        _ => bail!(
            "環境変数 NEON_API_KEY が未設定です。Neon の API key を環境変数で\
             渡してください (.env は読みません)。"
        ),
    }
}

fn neon_context(context: &Context) -> Result<&NeonContext> {
    match context.neon.as_ref() {
        Some(neon) => Ok(neon),
        None => bail!("neon is not configured for this stage"),
    }
}

/// API の client を作る (key を読む)。テストでは stub server に向けたものを渡す
pub type ApiFactory<'f> = &'f dyn Fn() -> Result<NeonApi>;

pub async fn status(
    api: &NeonApi,
    neon: &NeonContext,
    stage: &str,
    sink: &mut dyn Sink,
) -> Result<()> {
    let mut r = Neon::new(api, neon);
    // project が無ければ project_id が止める (Python 版と同じ)
    r.project_id(sink).await?;
    sink.success("Project found");
    if r.branch(sink).await?.is_none() {
        sink.warning("Branch not found");
        return Ok(());
    }
    sink.success("Branch found");
    if !r.database(sink).await? {
        sink.warning("Database not found");
        return Ok(());
    }
    sink.success("Database found");
    let endpoint = r.endpoint(sink).await?;
    match endpoint.as_ref() {
        Some(endpoint) => sink.success(&format!("Endpoint found: {}", endpoint.host)),
        None => sink.warning("Endpoint not found"),
    }
    let role = r.role(sink).await?;
    match role {
        Some(_) => sink.success(&format!("Role found: {}", neon.role_name)),
        None => sink.warning("Role not found"),
    }
    if role.is_some() && endpoint.is_some() {
        // password は出さない (status は状態の確認用。session log などに secret を残さない)
        let masked = r.masked_database_url(sink).await?;
        sink.success(&format!("Database url: {masked}"));
        sink.info(&format!(
            "接続 URL (password 込み) は `pocket resource neon url --stage {stage}` を使用してください。"
        ));
    }
    Ok(())
}

pub async fn branch_out(
    api: &NeonApi,
    neon: &NeonContext,
    base: &NeonContext,
    sink: &mut dyn Sink,
) -> Result<()> {
    let mut r = Neon::new(api, neon);
    if r.branch(sink).await?.is_some() {
        bail!("Branch already exists");
    }
    let mut base_neon = Neon::new(api, base);
    if !base_neon.working(sink).await? {
        bail!("Base stage is not working");
    }
    let Some(base_branch) = base_neon.branch(sink).await? else {
        bail!("base stage branch is not resolved");
    };
    r.create_branch(Some(&base_branch), sink).await?;
    sink.success("New branch was created");
    Ok(())
}

pub async fn delete(
    api: &NeonApi,
    neon: &NeonContext,
    prompt: &impl Prompt,
    sink: &mut dyn Sink,
) -> Result<()> {
    let mut r = Neon::new(api, neon);
    if r.branch(sink).await?.is_none() {
        bail!("Neon branch '{}' が見つかりません", neon.branch_name);
    }
    match r.destroy_plan(sink).await? {
        DestroyPlan::Blocked => bail!(
            "branch '{}' は root branch のため単体削除できません (Neon 仕様)。\
             project '{}' には他の branch が残っているため project 削除も行いません。\
             他の branch を先に削除してください。",
            neon.branch_name,
            neon.project_name
        ),
        DestroyPlan::Project => {
            let text = format!(
                "branch '{}' は root branch のため、project '{}' を丸ごと削除します。\
                 よろしいですか？",
                neon.branch_name, neon.project_name
            );
            if !prompt.confirm(&text, false)? {
                bail!("Aborted!");
            }
            r.delete_project(sink).await?;
            sink.success(&format!("Project '{}' was deleted.", neon.project_name));
        }
        DestroyPlan::Branch => {
            r.delete_branch(sink).await?;
            sink.success("Branch was deleted successfully.");
        }
    }
    Ok(())
}

/// 正準名の値の読み書き (store-url / url)
pub trait UrlStore {
    async fn exists(&self, name: &str, store: StoreType, region: &str) -> Result<bool>;
    async fn read(&self, name: &str, store: StoreType, region: &str) -> Result<Option<String>>;
    async fn put(&self, name: &str, store: StoreType, value: &str, region: &str) -> Result<()>;
}

struct AwsUrlStore<'a> {
    sdk: &'a aws_config::SdkConfig,
}

impl UrlStore for AwsUrlStore<'_> {
    async fn exists(&self, name: &str, store: StoreType, region: &str) -> Result<bool> {
        AwsSecretStore::new(self.sdk.clone())
            .user_secret_exists(name, store, region)
            .await
    }

    async fn read(&self, name: &str, store: StoreType, region: &str) -> Result<Option<String>> {
        AwsStoredValues { sdk: self.sdk }
            .read(name, store, region)
            .await
    }

    async fn put(&self, name: &str, store: StoreType, value: &str, region: &str) -> Result<()> {
        AwsStoredValues { sdk: self.sdk }
            .put(name, store, value, region)
            .await?;
        Ok(())
    }
}

fn is_neon_url(spec: &UserSecretSpec) -> bool {
    spec.r#type == Some(UserSecretType::NeonDatabaseUrl)
}

fn type_label(spec: &UserSecretSpec) -> &'static str {
    match spec.r#type {
        Some(UserSecretType::NeonDatabaseUrl) => SECRET_TYPE,
        None => "None",
    }
}

fn store_of(sc: &SecretsContext, spec: &UserSecretSpec) -> StoreType {
    spec.store.unwrap_or(sc.store)
}

/// `store-url` の本体 (Python の `run_store_url`)
pub async fn store_url(
    store: &impl UrlStore,
    context: &Context,
    stage: &str,
    key: Option<&str>,
    force: bool,
    make_api: ApiFactory<'_>,
    sink: &mut dyn Sink,
) -> Result<()> {
    let Some(sc) = context.secrets.as_ref() else {
        bail!(
            "container の secrets が設定されていません。\
             [container.<name>.secrets.user] に DATABASE_URL を宣言してください。"
        );
    };
    let target_key = match key {
        Some(key) => {
            let Some(spec) = sc.user.get(key) else {
                bail!("secrets.user に '{key}' がありません。");
            };
            if !is_neon_url(spec) {
                bail!(
                    "secrets.user '{key}' は type={SECRET_TYPE} ではありません (type={})。",
                    type_label(spec)
                );
            }
            key.to_string()
        }
        None => {
            let candidates: Vec<&String> = sc
                .user
                .iter()
                .filter(|(_, spec)| is_neon_url(spec))
                .map(|(key, _)| key)
                .collect();
            match candidates.as_slice() {
                [] => bail!(
                    "type={SECRET_TYPE} の stored user secret が宣言されていません。\
                     [container.<name>.secrets.user] に \
                     `DATABASE_URL = {{ type = \"{SECRET_TYPE}\" }}` を追加してください。"
                ),
                [one] => (*one).clone(),
                many => bail!(
                    "type={SECRET_TYPE} の user secret が複数あります ({})。--key で指定してください。",
                    many.iter().map(|k| k.as_str()).collect::<Vec<_>>().join(", ")
                ),
            }
        }
    };
    let spec = sc.user.get(&target_key).expect("選んだ key");
    let name = spec.name.clone().unwrap_or_default();
    let spec_store = store_of(sc, spec);
    if !force && !name.is_empty() && store.exists(&name, spec_store, &sc.region).await? {
        sink.warning(&format!(
            "{target_key} は既に存在します ({name})。rotate する場合は --force を付けてください。"
        ));
        return Ok(());
    }
    let Some(neon) = context.neon.as_ref() else {
        bail!("neon が pocket.toml に宣言されていません (store-url 不可)");
    };
    let api = make_api()?;
    let info = ensure_url(&api, neon, sink).await?;
    sink.info(&format!(
        "stage={stage} → project={} → branch={} → endpoint={}",
        neon.project_name, neon.branch_name, info.endpoint_host
    ));
    if info.branch_created {
        sink.warning(&format!(
            "branch '{}' はこの実行で新規作成されました (空の branch)。既存データ\
             へ接続するつもりなら、pocket.toml の [<stage>.neon] branch_name が\
             意図した branch を指しているか確認してください。",
            neon.branch_name
        ));
    }
    if name.is_empty() {
        bail!("user secret name is not resolved");
    }
    store.put(&name, spec_store, &info.url, &sc.region).await?;
    sink.success(&format!(
        "{DB_LABEL} URL を {target_key} ({name}) に保存しました。"
    ));
    Ok(())
}

/// `url` の本体 (Python の `run_get_url`)。stdout には URL だけを出す
pub async fn url(
    store: &impl UrlStore,
    context: &Context,
    live: bool,
    make_api: ApiFactory<'_>,
    sink: &mut dyn Sink,
) -> Result<()> {
    if !live {
        if let Some(stored) = read_stored_url(store, context).await? {
            sink.print(&stored);
            return Ok(());
        }
        sink.log(&format!(
            "{DB_LABEL}: stored user secret が未 provision のため provider API で live 算出します。"
        ));
    }
    let Some(neon) = context.neon.as_ref() else {
        bail!("neon が pocket.toml に宣言されていません (live 算出不可)");
    };
    let api = make_api()?;
    let url = Neon::new(&api, neon).database_url(sink).await?;
    sink.print(&url);
    Ok(())
}

/// `type=neon_database_url` の stored URL。宣言があればその名前・store で、無ければ type
/// 基準の正準名で読む。どちらも無ければ `None`
async fn read_stored_url(store: &impl UrlStore, context: &Context) -> Result<Option<String>> {
    let Some(sc) = context.secrets.as_ref() else {
        return Ok(None);
    };
    let specs: Vec<&UserSecretSpec> = sc.user.values().filter(|spec| is_neon_url(spec)).collect();
    match specs.as_slice() {
        [] => {
            let name = magic_pocket_settings::context::user_secret_path(
                &sc.pocket_key,
                SECRET_TYPE,
                sc.store,
            );
            store.read(&name, sc.store, &sc.region).await
        }
        [spec] => match spec.name.as_deref() {
            Some(name) => store.read(name, store_of(sc, spec), &sc.region).await,
            None => Ok(None),
        },
        _ => bail!("type={SECRET_TYPE} の stored user secret が複数宣言されているため曖昧です。"),
    }
}

pub async fn run(command: NeonCommand, sink: &mut dyn Sink) -> Result<()> {
    let make_api: ApiFactory = &|| NeonApi::new(&neon_api_key()?);
    match command {
        NeonCommand::Context(stage) => {
            let (context, _) = crate::context::load_context_read_only(&stage.stage)?;
            let neon = neon_context(&context)?;
            let dump = mask_secret_values(&serde_json::to_value(neon)?);
            sink.print(&pyrepr::pformat(&dump));
            Ok(())
        }
        NeonCommand::Status(stage) => {
            let (context, _) = crate::context::load_context_read_only(&stage.stage)?;
            let neon = neon_context(&context)?;
            status(&make_api()?, neon, &stage.stage, sink).await
        }
        NeonCommand::Create(stage) => {
            let (context, _) = crate::context::load_context(&stage.stage)?;
            let neon = neon_context(&context)?;
            Neon::new(&make_api()?, neon).create(sink).await?;
            sink.success("New branch was created");
            Ok(())
        }
        NeonCommand::ResetDatabase(stage) => {
            let (context, _) = crate::context::load_context(&stage.stage)?;
            let neon = neon_context(&context)?;
            Neon::new(&make_api()?, neon).reset_database(sink).await?;
            sink.success("Reset database");
            Ok(())
        }
        NeonCommand::BranchOut { stage, base_stage } => {
            let (context, _) = crate::context::load_context(&stage.stage)?;
            let neon = neon_context(&context)?;
            let api = make_api()?;
            let (base_context, _) = crate::context::load_context(&base_stage)?;
            let base = neon_context(&base_context)?;
            branch_out(&api, neon, base, sink).await
        }
        NeonCommand::Delete { stage, yes } => {
            let prompt = Interaction { assume_yes: yes };
            let text = format!(
                "stage '{}' の Neon ブランチを削除しますか？(データは失われます)",
                stage.stage
            );
            if !prompt.confirm(&text, false)? {
                bail!("Aborted!");
            }
            let (context, _) = crate::context::load_context(&stage.stage)?;
            let neon = neon_context(&context)?;
            delete(&make_api()?, neon, &prompt, sink).await
        }
        NeonCommand::StoreUrl { stage, key, force } => {
            let (context, _) = crate::context::load_context(&stage.stage)?;
            let sdk = crate::aws_util::load_sdk().await;
            let store = AwsUrlStore { sdk: &sdk };
            store_url(
                &store,
                &context,
                &stage.stage,
                key.as_deref(),
                force,
                make_api,
                sink,
            )
            .await
        }
        NeonCommand::Url { stage, live } => {
            let (context, _) = crate::context::load_context_read_only(&stage.stage)?;
            let sdk = crate::aws_util::load_sdk().await;
            let store = AwsUrlStore { sdk: &sdk };
            url(&store, &context, live, make_api, sink).await
        }
    }
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;
    use std::collections::BTreeMap;
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    use serde_json::json;

    use super::*;
    use crate::deploy::interaction::testing::FakePrompt;
    use crate::deploy::testing::{Reply, Request, StubServer};
    use crate::stack::testing::context_from_toml;
    use crate::status::sink::{Recorded, Stream, Style};

    const KEY: &str = "neon-secret-key-123";

    fn ctx(parent: Option<&str>) -> NeonContext {
        NeonContext {
            pg_version: 17,
            project_name: "app".to_string(),
            branch_name: "dev".to_string(),
            branch_name_specified: true,
            stage: Some("dev".to_string()),
            parent_branch_name: parent.map(str::to_string),
            name: "appdb".to_string(),
            role_name: "app user".to_string(),
        }
    }

    /// Neon の API の状態
    #[derive(Default, Clone)]
    struct State {
        branch: bool,
        /// dev branch が root (parent_id 無し)
        root: bool,
        /// main 以外の branch が他にもある
        others: bool,
        role: bool,
        database: bool,
        endpoint: bool,
    }

    const BRANCHES: &str = "/neon/projects/p1/branches";
    const ROLES: &str = "/neon/projects/p1/branches/br-dev/roles";
    const DATABASES: &str = "/neon/projects/p1/branches/br-dev/databases";

    fn server(initial: State) -> StubServer {
        let state = Arc::new(Mutex::new(initial));
        StubServer::start(move |request: &Request| {
            let mut state = state.lock().unwrap();
            let role_path = format!("{ROLES}/app%20user");
            let reveal_path = format!("{role_path}/reveal_password");
            match (request.method.as_str(), request.path.as_str()) {
                ("GET", "/neon/projects") => Reply::ok(json!({
                    "projects": [{"id": "p0", "name": "other"}, {"id": "p1", "name": "app"}]
                })),
                ("GET", BRANCHES) => {
                    let mut branches = Vec::new();
                    if !state.root || state.others {
                        branches.push(json!({"id": "br-main", "name": "main"}));
                    }
                    if state.branch {
                        let mut dev = json!({"id": "br-dev", "name": "dev"});
                        if !state.root {
                            dev["parent_id"] = json!("br-main");
                        }
                        branches.push(dev);
                    }
                    Reply::ok(json!({"branches": branches}))
                }
                ("POST", BRANCHES) => {
                    state.branch = true;
                    state.endpoint = true;
                    Reply::json(201, json!({}))
                }
                ("DELETE", "/neon/projects/p1/branches/br-dev") => {
                    state.branch = false;
                    Reply::ok(json!({}))
                }
                ("DELETE", "/neon/projects/p1/endpoints/ep-dev") => {
                    state.endpoint = false;
                    Reply::ok(json!({}))
                }
                ("DELETE", "/neon/projects/p1") => Reply::ok(json!({})),
                ("GET", path) if path == role_path => {
                    if state.role {
                        Reply::ok(json!({"role": {"name": "app user"}}))
                    } else {
                        Reply::json(404, json!({"message": "role not found"}))
                    }
                }
                ("POST", ROLES) => {
                    state.role = true;
                    Reply::json(201, json!({}))
                }
                ("GET", path) if path == reveal_path => Reply::ok(json!({"password": "p@ss/w:rd"})),
                ("GET", DATABASES) => {
                    let databases = if state.database {
                        vec![json!({"name": "appdb", "owner_name": "app user"})]
                    } else {
                        vec![]
                    };
                    Reply::ok(json!({"databases": databases}))
                }
                ("POST", DATABASES) => {
                    state.database = true;
                    Reply::json(201, json!({}))
                }
                ("DELETE", "/neon/projects/p1/branches/br-dev/databases/appdb") => {
                    state.database = false;
                    Reply::ok(json!({}))
                }
                ("GET", "/neon/projects/p1/endpoints") => {
                    let mut endpoints = vec![
                        json!({"id": "ep-main", "branch_id": "br-main", "type": "read_write", "host": "main.host"}),
                        json!({"id": "ep-ro", "branch_id": "br-dev", "type": "read_only", "host": "ro.host"}),
                    ];
                    if state.endpoint {
                        endpoints.push(
                            json!({"id": "ep-dev", "branch_id": "br-dev", "type": "read_write", "host": "rw.host"}),
                        );
                    }
                    Reply::ok(json!({"endpoints": endpoints}))
                }
                _ => Reply::json(500, json!({"message": "unexpected request"})),
            }
        })
    }

    fn api(server: &StubServer) -> NeonApi {
        NeonApi::with_endpoint(KEY, &format!("{}/neon/", server.url), Duration::ZERO).unwrap()
    }

    fn ready() -> State {
        State {
            branch: true,
            role: true,
            database: true,
            endpoint: true,
            ..State::default()
        }
    }

    fn writes(server: &StubServer) -> Vec<String> {
        server.writes().iter().map(Request::line).collect()
    }

    fn texts(sink: &Recorded) -> Vec<&str> {
        sink.0.iter().map(|(_, _, t)| t.as_str()).collect()
    }

    #[tokio::test]
    async fn test_create_makes_branch_role_and_database_with_auth() {
        let server = server(State::default());
        let api = api(&server);
        let neon = ctx(None);
        Neon::new(&api, &neon)
            .create(&mut Recorded::default())
            .await
            .unwrap();
        let w = server.writes();
        assert_eq!(
            writes(&server),
            [
                format!("POST {BRANCHES}"),
                format!("POST {ROLES}"),
                format!("POST {DATABASES}")
            ]
        );
        assert_eq!(
            w[0].json(),
            json!({"branch": {"name": "dev"}, "endpoints": [{"type": "read_write"}]})
        );
        assert_eq!(w[1].json(), json!({"role": {"name": "app user"}}));
        assert_eq!(
            w[2].json(),
            json!({"database": {"name": "appdb", "owner_name": "app user"}})
        );
        assert_eq!(w[0].header("content-type"), Some("application/json"));
        for request in server.requests() {
            assert_eq!(
                request.header("authorization"),
                Some(&*format!("Bearer {KEY}"))
            );
            assert_eq!(request.header("accept"), Some("application/json"));
        }
        // role を作った後に password を reveal する
        let lines = server.lines();
        let at = |line: &str| lines.iter().position(|l| l == line).unwrap();
        assert!(
            at(&format!("POST {ROLES}")) < at(&format!("GET {ROLES}/app%20user/reveal_password"))
        );
    }

    #[tokio::test]
    async fn test_create_with_parent_and_existing_parts() {
        let server = server(State::default());
        let api = api(&server);
        let neon = ctx(Some("main"));
        Neon::new(&api, &neon)
            .create(&mut Recorded::default())
            .await
            .unwrap();
        assert_eq!(
            server.writes()[0].json(),
            json!({"branch": {"name": "dev", "parent_id": "br-main"}, "endpoints": [{"type": "read_write"}]})
        );
        // 親が見つからなければ何も作らない
        let server2 = self::server(State::default());
        let api2 = self::api(&server2);
        let missing = ctx(Some("staging"));
        let error = Neon::new(&api2, &missing)
            .create(&mut Recorded::default())
            .await
            .unwrap_err();
        assert_eq!(
            error.to_string(),
            "Neon parent branch 'staging' not found in project 'app'"
        );
        assert!(server2.writes().is_empty());
        // 全部あれば何も書かない
        let server3 = self::server(ready());
        let api3 = self::api(&server3);
        Neon::new(&api3, &missing)
            .create(&mut Recorded::default())
            .await
            .unwrap();
        assert!(server3.writes().is_empty());
    }

    #[tokio::test]
    async fn test_database_url_and_status() {
        let server = server(ready());
        let api = api(&server);
        let neon = ctx(None);
        let url = Neon::new(&api, &neon)
            .database_url(&mut Recorded::default())
            .await
            .unwrap();
        assert_eq!(
            url,
            "postgres://app%20user:p%40ss%2Fw%3Ard@rw.host:5432/appdb?sslmode=require"
        );
        let mut sink = Recorded::default();
        status(&api, &neon, "dev", &mut sink).await.unwrap();
        assert_eq!(
            texts(&sink),
            [
                "Project found",
                "Branch found",
                "Database found",
                "Endpoint found: rw.host",
                "Role found: app user",
                "Database url: postgres://app%20user:****@rw.host:5432/appdb?sslmode=require",
                "接続 URL (password 込み) は `pocket resource neon url --stage dev` を使用してください。",
            ]
        );
        // status は password を reveal しない (database_url の 1 回だけ)
        let reveals = server
            .lines()
            .iter()
            .filter(|l| l.ends_with("reveal_password"))
            .count();
        assert_eq!(reveals, 1);

        let server = self::server(State {
            branch: true,
            ..State::default()
        });
        let api = self::api(&server);
        let mut sink = Recorded::default();
        status(&api, &neon, "dev", &mut sink).await.unwrap();
        assert_eq!(
            texts(&sink),
            ["Project found", "Branch found", "Database not found"]
        );
        let error = Neon::new(&api, &neon)
            .database_url(&mut sink)
            .await
            .unwrap_err();
        assert_eq!(error.to_string(), "Create role and endpoint first");
    }

    #[tokio::test]
    async fn test_branch_out_and_reset_database() {
        let server = server(ready());
        let api = api(&server);
        let base = ctx(None);
        let mut other = ctx(None);
        other.branch_name = "feature".to_string();
        let mut sink = Recorded::default();
        branch_out(&api, &other, &base, &mut sink).await.unwrap();
        assert_eq!(
            server.writes()[0].json(),
            json!({"branch": {"name": "feature", "parent_id": "br-dev"}, "endpoints": [{"type": "read_write"}]})
        );
        assert_eq!(texts(&sink), ["New branch was created"]);
        // 既にある branch には分岐しない
        let error = branch_out(&api, &base, &base, &mut sink).await.unwrap_err();
        assert_eq!(error.to_string(), "Branch already exists");
        // 分岐元が揃っていなければ止める
        let server = self::server(State {
            branch: true,
            ..State::default()
        });
        let api = self::api(&server);
        let error = branch_out(&api, &other, &base, &mut sink)
            .await
            .unwrap_err();
        assert_eq!(error.to_string(), "Base stage is not working");
        assert!(server.writes().is_empty());

        let server = self::server(ready());
        let api = self::api(&server);
        Neon::new(&api, &base)
            .reset_database(&mut sink)
            .await
            .unwrap();
        assert_eq!(
            writes(&server),
            [
                format!("DELETE {DATABASES}/appdb"),
                format!("POST {DATABASES}")
            ]
        );
    }

    #[tokio::test]
    async fn test_delete_plans() {
        let neon = ctx(None);
        // 子 branch: endpoint → branch の順に消す
        let server = server(ready());
        let api = api(&server);
        let mut sink = Recorded::default();
        delete(&api, &neon, &FakePrompt::yes(), &mut sink)
            .await
            .unwrap();
        assert_eq!(
            writes(&server),
            [
                "DELETE /neon/projects/p1/endpoints/ep-dev",
                "DELETE /neon/projects/p1/branches/br-dev"
            ]
        );
        assert_eq!(texts(&sink), ["Branch was deleted successfully."]);
        // root branch だけの project: 確認して project ごと
        let server = self::server(State {
            root: true,
            ..ready()
        });
        let api = self::api(&server);
        let prompt = FakePrompt::answering(true);
        let mut sink = Recorded::default();
        delete(&api, &neon, &prompt, &mut sink).await.unwrap();
        assert_eq!(writes(&server), ["DELETE /neon/projects/p1"]);
        assert!(prompt.asked.borrow()[0].contains("project 'app' を丸ごと削除します"));
        // 断れば消さない
        let server = self::server(State {
            root: true,
            ..ready()
        });
        let api = self::api(&server);
        let error = delete(&api, &neon, &FakePrompt::answering(false), &mut sink)
            .await
            .unwrap_err();
        assert_eq!(error.to_string(), "Aborted!");
        assert!(server.writes().is_empty());
        // root branch に他の branch が同居: 消さない
        let server = self::server(State {
            root: true,
            others: true,
            ..ready()
        });
        let api = self::api(&server);
        let error = delete(&api, &neon, &FakePrompt::yes(), &mut sink)
            .await
            .unwrap_err();
        assert!(error
            .to_string()
            .contains("は root branch のため単体削除できません"));
        assert!(server.writes().is_empty());
        // branch が無い
        let server = self::server(State::default());
        let api = self::api(&server);
        let error = delete(&api, &neon, &FakePrompt::yes(), &mut sink)
            .await
            .unwrap_err();
        assert_eq!(error.to_string(), "Neon branch 'dev' が見つかりません");
    }

    #[tokio::test]
    async fn test_api_errors_hide_the_key() {
        let server = StubServer::start(|request: &Request| match request.path.as_str() {
            "/neon/projects" => Reply::ok(json!({"projects": [{"id": "p1", "name": "app"}]})),
            BRANCHES if request.method == "GET" => Reply::ok(json!({"branches": []})),
            BRANCHES => Reply::json(422, json!({"message": "branch limit exceeded"})),
            _ => Reply::json(401, json!({"message": "unauthorized"})),
        });
        let api = api(&server);
        let neon = ctx(None);
        let mut sink = Recorded::default();
        let error = Neon::new(&api, &neon).create(&mut sink).await.unwrap_err();
        assert_eq!(error.to_string(), "422: branch limit exceeded");
        // 401 は key の先頭と長さだけを stdout に出す
        let error = api
            .get("projects/p1/endpoints", &mut sink)
            .await
            .unwrap_err();
        assert_eq!(error.to_string(), "401: unauthorized");
        assert_eq!(
            sink.0,
            [
                (
                    Stream::Stdout,
                    Style::Plain,
                    "Used API key: neon-...".to_string()
                ),
                (
                    Stream::Stdout,
                    Style::Plain,
                    format!("API key length: {}", KEY.len())
                ),
            ]
        );
        let mut other = ctx(None);
        other.project_name = "missing".to_string();
        let error = Neon::new(&api, &other).branch(&mut sink).await.unwrap_err();
        assert_eq!(error.to_string(), "Neon project 'missing' not found");

        // project に限った key: 一覧のエラーにある project の id から引く
        let server = StubServer::start(|request: &Request| match request.path.as_str() {
            "/neon/projects" => Reply::json(
                403,
                json!({"message": "not allowed subject_project_id:\"p9\" for org"}),
            ),
            "/neon/projects/p9" => Reply::ok(json!({"project": {"id": "p9", "name": "app"}})),
            _ => Reply::json(500, json!({})),
        });
        let api = self::api(&server);
        let id = Neon::new(&api, &neon).project_id(&mut sink).await.unwrap();
        assert_eq!(id, "p9");
        let error = Neon::new(&api, &other)
            .project_id(&mut sink)
            .await
            .unwrap_err();
        assert_eq!(
            error.to_string(),
            "Neon project name mismatch: config='missing', actual='app'"
        );
    }

    #[derive(Default)]
    struct FakeStore {
        values: RefCell<BTreeMap<String, String>>,
        puts: RefCell<Vec<String>>,
    }

    impl UrlStore for FakeStore {
        async fn exists(&self, name: &str, _store: StoreType, _region: &str) -> Result<bool> {
            Ok(self.values.borrow().contains_key(name))
        }
        async fn read(
            &self,
            name: &str,
            _store: StoreType,
            _region: &str,
        ) -> Result<Option<String>> {
            Ok(self.values.borrow().get(name).cloned())
        }
        async fn put(
            &self,
            name: &str,
            store: StoreType,
            value: &str,
            _region: &str,
        ) -> Result<()> {
            self.puts.borrow_mut().push(format!("{name} {store:?}"));
            self.values
                .borrow_mut()
                .insert(name.to_string(), value.to_string());
            Ok(())
        }
    }

    fn neon_toml(user: &str) -> Context {
        let mut context = context_from_toml(
            &format!(
                "[general]\nregion = \"ap-northeast-1\"\nproject_name = \"app\"\nstages = [\"dev\"]\n\n\
                 [neon]\nproject_name = \"app\"\nbranch_name = \"dev\"\n\n\
                 [container.main]\ndockerfile_path = \"Dockerfile\"\n\n\
                 [container.main.handlers.wsgi]\ncommand = \"app.wsgi\"\n\n\
                 [container.main.secrets.user]\n{user}\n"
            ),
            "dev",
        );
        // stub server の role / database に合わせる (pocket.toml では導出される値)
        let neon = context.neon.as_mut().expect("neon");
        neon.role_name = "app user".to_string();
        neon.name = "appdb".to_string();
        context
    }

    #[tokio::test]
    async fn test_store_url_and_url() {
        let context = neon_toml("DATABASE_URL = { type = \"neon_database_url\" }");
        let sc = context.secrets.as_ref().expect("secrets");
        let name = sc.user.get("DATABASE_URL").unwrap().name.clone().unwrap();
        let server = server(State::default());
        let make: ApiFactory = &|| Ok(api(&server));
        let store = FakeStore::default();
        let mut sink = Recorded::default();
        store_url(&store, &context, "dev", None, false, make, &mut sink)
            .await
            .unwrap();
        let url = "postgres://app%20user:p%40ss%2Fw%3Ard@rw.host:5432/appdb?sslmode=require";
        assert_eq!(
            store.values.borrow().get(&name).map(String::as_str),
            Some(url)
        );
        assert_eq!(
            texts(&sink)[0],
            "stage=dev → project=app → branch=dev → endpoint=rw.host"
        );
        assert!(texts(&sink)[1].starts_with("branch 'dev' はこの実行で新規作成されました"));
        assert_eq!(
            *texts(&sink).last().unwrap(),
            format!("Neon URL を DATABASE_URL ({name}) に保存しました。")
        );
        // 既にあれば --force なしでは触らない (API も呼ばない)
        let before = server.lines().len();
        let mut sink = Recorded::default();
        store_url(&store, &context, "dev", None, false, make, &mut sink)
            .await
            .unwrap();
        assert_eq!(server.lines().len(), before);
        assert!(texts(&sink)[0].starts_with("DATABASE_URL は既に存在します"));
        let error = store_url(&store, &context, "dev", Some("NOPE"), true, make, &mut sink)
            .await
            .unwrap_err();
        assert_eq!(error.to_string(), "secrets.user に 'NOPE' がありません。");

        // url: stored を stdout に出す (API を呼ばない)
        let before = server.lines().len();
        let mut sink = Recorded::default();
        self::url(&store, &context, false, make, &mut sink)
            .await
            .unwrap();
        assert_eq!(sink.0, [(Stream::Stdout, Style::Plain, url.to_string())]);
        assert_eq!(server.lines().len(), before);
        // 未 provision なら live で出す (案内は stderr)
        let empty = FakeStore::default();
        let mut sink = Recorded::default();
        self::url(&empty, &context, false, make, &mut sink)
            .await
            .unwrap();
        let stdout: Vec<&String> = sink
            .0
            .iter()
            .filter(|r| r.0 == Stream::Stdout)
            .map(|r| &r.2)
            .collect();
        assert_eq!(stdout, [url]);
        assert!(sink.0[0]
            .2
            .starts_with("Neon: stored user secret が未 provision"));
    }

    #[tokio::test]
    async fn test_store_url_requires_typed_user_secret() {
        let server = server(State::default());
        let make: ApiFactory = &|| Ok(api(&server));
        let store = FakeStore::default();
        let mut sink = Recorded::default();
        let context = neon_toml("OTHER = { name = \"/x/other\" }");
        let error = store_url(&store, &context, "dev", None, false, make, &mut sink)
            .await
            .unwrap_err();
        assert!(error
            .to_string()
            .starts_with("type=neon_database_url の stored user secret が宣言されていません。"));
        let error = store_url(
            &store,
            &context,
            "dev",
            Some("OTHER"),
            false,
            make,
            &mut sink,
        )
        .await
        .unwrap_err();
        assert_eq!(
            error.to_string(),
            "secrets.user 'OTHER' は type=neon_database_url ではありません (type=None)。"
        );
        assert!(server.requests().is_empty());
        // 宣言が無くても type 基準の正準名で読める
        let sc = context.secrets.as_ref().unwrap();
        let canonical = magic_pocket_settings::context::user_secret_path(
            &sc.pocket_key,
            "neon_database_url",
            sc.store,
        );
        store
            .values
            .borrow_mut()
            .insert(canonical, "postgres://stored".to_string());
        self::url(&store, &context, false, make, &mut sink)
            .await
            .unwrap();
        assert_eq!(sink.0.last().unwrap().2, "postgres://stored");
    }
}
