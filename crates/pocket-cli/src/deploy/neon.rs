//! Neon の branch / role / database の確保と接続 URL (Python 版 `pocket.provisioning.neon`
//! の `Neon.create` / `Neon.database_url`)。
//!
//! project は作らない (名前で探し、無ければ止める)。branch が既にあれば (project の
//! default branch を含む) その上に role と database を足すだけで、branch の作成要求は
//! 出さない。削除系は destroy と個別コマンドの移植で扱う

use anyhow::{bail, Result};
use magic_pocket_settings::context::NeonContext;
use reqwest::Method;
use serde_json::{json, Value as Json};

use crate::status::providers::{
    find_by, neon_error_detail, neon_get, neon_list, neon_project_id, neon_stage_branch_warning,
    Auth, Http,
};
use crate::status::sink::Sink;

/// Python の `urllib.parse.quote(text, safe="")`
pub fn quote(text: &str) -> String {
    let mut out = String::new();
    for byte in text.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b'-' | b'~') {
            out.push(byte as char);
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

/// branch 作成の body。`parent_id` が無ければ Neon の default branch から分かれる
pub fn branch_body(neon: &NeonContext, parent_id: Option<&str>) -> Json {
    let mut branch = json!({"name": neon.branch_name});
    if let Some(parent_id) = parent_id {
        branch["parent_id"] = json!(parent_id);
    }
    json!({"branch": branch, "endpoints": [{"type": "read_write"}]})
}

pub fn role_body(neon: &NeonContext) -> Json {
    json!({"role": {"name": neon.role_name}})
}

pub fn database_body(neon: &NeonContext) -> Json {
    json!({"database": {"name": neon.name, "owner_name": neon.role_name}})
}

/// stage の branch の消し方
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DestroyPlan {
    /// root でない branch。branch 単位で消せる
    Branch,
    /// root branch で、project に他の branch が無い。project ごと消す
    Project,
    /// root branch で、他の branch が同居している。何も消せない
    Blocked,
}

/// branch の一覧から消し方を決める。`branch_name` の branch が無ければ `None`
pub fn destroy_plan(branches: &Json, branch_name: &str) -> Option<DestroyPlan> {
    let branch = find_by(branches, "name", branch_name)?;
    if branch["parent_id"].as_str().is_some() {
        return Some(DestroyPlan::Branch);
    }
    let others = branches
        .as_array()
        .is_some_and(|list| list.iter().any(|b| b["id"] != branch["id"]));
    Some(if others {
        DestroyPlan::Blocked
    } else {
        DestroyPlan::Project
    })
}

pub struct Neon<'a> {
    pub http: &'a Http,
    pub ctx: &'a NeonContext,
}

impl Neon<'_> {
    fn key(&self) -> &str {
        self.ctx.api_key.as_deref().unwrap_or_default()
    }

    /// 書き込み (POST)。成功したら Neon 側の反映を待つ
    async fn post(&self, path: &str, body: &Json, sink: &mut dyn Sink) -> Result<()> {
        self.write(Method::POST, path, Some(body), sink).await
    }

    async fn write(
        &self,
        method: Method,
        path: &str,
        body: Option<&Json>,
        sink: &mut dyn Sink,
    ) -> Result<()> {
        let url = format!("{}{path}", self.http.endpoints.neon);
        let key = self.key();
        let response = self
            .http
            .request(method, &url, Auth::Bearer(key), body)
            .await?;
        if response.ok() {
            self.http.settle().await;
            return Ok(());
        }
        if response.status == 401 {
            sink.print(&format!(
                "Used API key: {}...",
                key.chars().take(5).collect::<String>()
            ));
            sink.print(&format!("API key length: {}", key.chars().count()));
        }
        bail!("{}: {}", response.status, neon_error_detail(&response))
    }

    /// project の path と branch の一覧。stage 名の branch の警告は `warn` のときだけ出す
    async fn branches(&self, base: &str, warn: bool, sink: &mut dyn Sink) -> Result<Json> {
        let mut json = neon_list(self.http, self.key(), &format!("{base}/branches"), sink).await?;
        let branches = json["branches"].take();
        if warn {
            if let Some(warning) = neon_stage_branch_warning(self.ctx, &branches) {
                sink.warning(&warning);
            }
        }
        Ok(branches)
    }

    fn branch_id(&self, branches: &Json) -> Option<String> {
        find_by(branches, "name", &self.ctx.branch_name)
            .map(|branch| branch["id"].as_str().unwrap_or_default().to_string())
    }

    /// branch (無ければ作る) → role → database の順に確保する
    pub async fn create(&self, sink: &mut dyn Sink) -> Result<()> {
        let neon = self.ctx;
        let project_id = neon_project_id(self.http, self.key(), neon, sink).await?;
        let base = format!("projects/{project_id}");
        let mut branches = self.branches(&base, true, sink).await?;
        if self.branch_id(&branches).is_none() {
            // 親の指定があるのに見つからないときは止める (黙って default branch から
            // 分けると、意図しないデータの branch ができる)
            let parent = neon.parent_branch_name.as_deref().filter(|n| !n.is_empty());
            let parent_id = match parent {
                None => None,
                Some(name) => match find_by(&branches, "name", name) {
                    Some(branch) => Some(branch["id"].as_str().unwrap_or_default().to_string()),
                    None => bail!(
                        "Neon parent branch '{name}' not found in project '{}'",
                        neon.project_name
                    ),
                },
            };
            let body = branch_body(neon, parent_id.as_deref());
            self.post(&format!("{base}/branches"), &body, sink).await?;
            branches = self.branches(&base, false, sink).await?;
        }
        let Some(branch_id) = self.branch_id(&branches) else {
            bail!("branch not found");
        };
        let branch_base = format!("{base}/branches/{branch_id}");
        self.ensure_role(&branch_base, sink).await?;
        self.ensure_database(&branch_base, sink).await
    }

    async fn role(&self, branch_base: &str, sink: &mut dyn Sink) -> Result<Option<Json>> {
        let path = format!("{branch_base}/roles/{}", self.ctx.role_name);
        Ok(neon_get(self.http, self.key(), &path, sink)
            .await?
            .map(|mut json| json["role"].take()))
    }

    /// role の password。一覧や取得の応答に無ければ reveal の API で取る
    async fn role_password(
        &self,
        branch_base: &str,
        role: &Json,
        sink: &mut dyn Sink,
    ) -> Result<String> {
        if let Some(password) = role["password"].as_str() {
            return Ok(password.to_string());
        }
        let path = format!(
            "{branch_base}/roles/{}/reveal_password",
            role["name"].as_str().unwrap_or_default()
        );
        let json = neon_list(self.http, self.key(), &path, sink).await?;
        Ok(json["password"].as_str().unwrap_or_default().to_string())
    }

    async fn ensure_role(&self, branch_base: &str, sink: &mut dyn Sink) -> Result<()> {
        if self.role(branch_base, sink).await?.is_some() {
            return Ok(());
        }
        let path = format!("{branch_base}/roles");
        self.post(&path, &role_body(self.ctx), sink).await?;
        let Some(role) = self.role(branch_base, sink).await? else {
            bail!("Create role first");
        };
        self.role_password(branch_base, &role, sink).await?;
        Ok(())
    }

    async fn ensure_database(&self, branch_base: &str, sink: &mut dyn Sink) -> Result<()> {
        let path = format!("{branch_base}/databases");
        let databases = neon_list(self.http, self.key(), &path, sink).await?;
        if find_by(&databases["databases"], "name", &self.ctx.name).is_some() {
            return Ok(());
        }
        self.post(&path, &database_body(self.ctx), sink).await
    }

    /// stage の branch を消す (destroy)。
    ///
    /// root branch は branch 単位で消せない。project に他の branch が無ければ project ごと
    /// 消し、他の branch が在れば (他の stage の巻き添えになるので) 何も消さずに知らせる。
    /// branch が無ければ何もしない
    pub async fn destroy(&self, sink: &mut dyn Sink) -> Result<()> {
        let neon = self.ctx;
        let project_id = neon_project_id(self.http, self.key(), neon, sink).await?;
        let base = format!("projects/{project_id}");
        let branches = self.branches(&base, true, sink).await?;
        let Some(plan) = destroy_plan(&branches, &neon.branch_name) else {
            return Ok(());
        };
        match plan {
            DestroyPlan::Blocked => {
                sink.warning(&format!(
                    "Neon branch '{}' は root branch のため単体削除できず、project '{}' には\
                     他の branch が残っているため project 削除も行いません。\
                     他 stage の destroy 後に pocket resource neon delete で削除してください。",
                    neon.branch_name, neon.project_name
                ));
                Ok(())
            }
            DestroyPlan::Project => {
                sink.log("Destroying Neon project (root branch のため project ごと削除)...");
                self.write(Method::DELETE, &base, None, sink).await?;
                sink.success(&format!(
                    "Neon project '{}' was deleted.",
                    neon.project_name
                ));
                Ok(())
            }
            DestroyPlan::Branch => {
                sink.log("Destroying Neon branch...");
                let branch_id = self.branch_id(&branches).unwrap_or_default();
                let path = format!("{base}/endpoints");
                let endpoints = neon_list(self.http, self.key(), &path, sink).await?;
                let endpoint = endpoints["endpoints"].as_array().and_then(|list| {
                    list.iter().find(|endpoint| {
                        endpoint["branch_id"].as_str() == Some(branch_id.as_str())
                            && endpoint["type"].as_str() == Some("read_write")
                    })
                });
                let Some(endpoint_id) = endpoint.and_then(|e| e["id"].as_str()) else {
                    bail!("Branch or endpoint not found. Something is wrong.");
                };
                let path = format!("{base}/endpoints/{endpoint_id}");
                self.write(Method::DELETE, &path, None, sink).await?;
                let path = format!("{base}/branches/{branch_id}");
                self.write(Method::DELETE, &path, None, sink).await?;
                sink.success("Neon branch was deleted.");
                Ok(())
            }
        }
    }

    /// destroy で何を消すことになるか (対象の一覧の表示用)。branch が無ければ `None`
    pub async fn destroy_plan(&self, sink: &mut dyn Sink) -> Result<Option<DestroyPlan>> {
        let project_id = neon_project_id(self.http, self.key(), self.ctx, sink).await?;
        let branches = self
            .branches(&format!("projects/{project_id}"), true, sink)
            .await?;
        Ok(destroy_plan(&branches, &self.ctx.branch_name))
    }

    /// 接続 URL。branch・role・書き込み用の endpoint のどれかが無ければ `None` (未準備)
    pub async fn database_url(&self, sink: &mut dyn Sink) -> Result<Option<String>> {
        let neon = self.ctx;
        let project_id = neon_project_id(self.http, self.key(), neon, sink).await?;
        let base = format!("projects/{project_id}");
        let branches = self.branches(&base, true, sink).await?;
        let Some(branch_id) = self.branch_id(&branches) else {
            return Ok(None);
        };
        let branch_base = format!("{base}/branches/{branch_id}");
        let Some(role) = self.role(&branch_base, sink).await? else {
            return Ok(None);
        };
        // read replica (read_only の endpoint) を足した branch でも、一覧の並びに
        // 依らず書き込み用を選ぶ
        let path = format!("{base}/endpoints");
        let endpoints = neon_list(self.http, self.key(), &path, sink).await?;
        let endpoint = endpoints["endpoints"].as_array().and_then(|list| {
            list.iter().find(|endpoint| {
                endpoint["branch_id"].as_str() == Some(branch_id.as_str())
                    && endpoint["type"].as_str() == Some("read_write")
            })
        });
        let Some(endpoint) = endpoint else {
            return Ok(None);
        };
        let password = self.role_password(&branch_base, &role, sink).await?;
        // 読む側 (pocket.django の db_url) は unquote するので、必ず quote する
        Ok(Some(format!(
            "postgres://{}:{}@{}:5432/{}?sslmode=require",
            quote(&neon.role_name),
            quote(&password),
            endpoint["host"].as_str().unwrap_or_default(),
            neon.name
        )))
    }
}

#[cfg(test)]
mod tests {
    use std::sync::{Arc, Mutex};

    use magic_pocket_settings::model::ProvisioningMode;

    use super::*;
    use crate::deploy::testing::{stub_http, Reply, Request, StubServer};
    use crate::status::sink::Recorded;

    fn context(parent: Option<&str>) -> NeonContext {
        NeonContext {
            pg_version: 17,
            api_key: Some("neon-key".to_string()),
            project_name: "app".to_string(),
            branch_name: "dev".to_string(),
            branch_name_specified: true,
            stage: Some("dev".to_string()),
            parent_branch_name: parent.map(str::to_string),
            name: "appdb".to_string(),
            role_name: "app user".to_string(),
            provisioning: ProvisioningMode::Deploy,
        }
    }

    /// Neon の API の状態 (branch・role・database が在るか)
    #[derive(Default, Clone)]
    struct State {
        branch: bool,
        role: bool,
        database: bool,
        endpoint: bool,
        /// branch の作成要求を受けても branch を作らない
        branch_never_appears: bool,
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
                    let mut branches = vec![json!({"id": "br-main", "name": "main"})];
                    if state.branch {
                        branches.push(json!({"id": "br-dev", "name": "dev"}));
                    }
                    Reply::ok(json!({"branches": branches}))
                }
                ("POST", BRANCHES) => {
                    state.branch = !state.branch_never_appears;
                    state.endpoint = true;
                    Reply::json(201, json!({}))
                }
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
                ("GET", "/neon/projects/p1/endpoints") => {
                    let mut endpoints = vec![
                        json!({"branch_id": "br-main", "type": "read_write", "host": "main.host"}),
                        json!({"branch_id": "br-dev", "type": "read_only", "host": "ro.host"}),
                    ];
                    if state.endpoint {
                        endpoints.push(
                            json!({"branch_id": "br-dev", "type": "read_write", "host": "rw.host"}),
                        );
                    }
                    Reply::ok(json!({"endpoints": endpoints}))
                }
                _ => Reply::json(500, json!({"message": "unexpected request"})),
            }
        })
    }

    fn write_lines(server: &StubServer) -> Vec<String> {
        server.writes().iter().map(Request::line).collect()
    }

    #[test]
    fn test_quote_matches_python() {
        assert_eq!(quote("abc-_.~XYZ019"), "abc-_.~XYZ019");
        assert_eq!(quote("p@ss/w:rd +"), "p%40ss%2Fw%3Ard%20%2B");
        assert_eq!(quote("日"), "%E6%97%A5");
    }

    #[tokio::test]
    async fn test_create_makes_branch_role_and_database() {
        let server = server(State::default());
        let http = stub_http(&server);
        let ctx = context(None);
        let mut sink = Recorded::default();
        Neon {
            http: &http,
            ctx: &ctx,
        }
        .create(&mut sink)
        .await
        .unwrap();
        let writes = server.writes();
        assert_eq!(
            write_lines(&server),
            [
                format!("POST {BRANCHES}"),
                format!("POST {ROLES}"),
                format!("POST {DATABASES}"),
            ]
        );
        // 親の指定が無ければ parent_id を送らない (Neon の default branch から分かれる)
        assert_eq!(
            writes[0].json(),
            json!({"branch": {"name": "dev"}, "endpoints": [{"type": "read_write"}]})
        );
        assert_eq!(writes[1].json(), json!({"role": {"name": "app user"}}));
        assert_eq!(
            writes[2].json(),
            json!({"database": {"name": "appdb", "owner_name": "app user"}})
        );
        for request in server.requests() {
            assert_eq!(request.header("authorization"), Some("Bearer neon-key"));
            assert_eq!(request.header("accept"), Some("application/json"));
        }
        assert_eq!(writes[0].header("content-type"), Some("application/json"));
        // role を作った後に password を reveal する
        let lines = server.lines();
        let reveal = format!("GET {ROLES}/app%20user/reveal_password");
        let at = |line: &str| lines.iter().position(|l| l == line).unwrap();
        assert!(at(&format!("POST {ROLES}")) < at(&reveal), "{lines:?}");
    }

    #[tokio::test]
    async fn test_create_branches_from_the_named_parent() {
        let server = server(State::default());
        let http = stub_http(&server);
        let ctx = context(Some("main"));
        Neon {
            http: &http,
            ctx: &ctx,
        }
        .create(&mut Recorded::default())
        .await
        .unwrap();
        assert_eq!(
            server.writes()[0].json(),
            json!({
                "branch": {"name": "dev", "parent_id": "br-main"},
                "endpoints": [{"type": "read_write"}],
            })
        );
    }

    #[tokio::test]
    async fn test_create_stops_when_parent_branch_is_missing() {
        let server = server(State::default());
        let http = stub_http(&server);
        let ctx = context(Some("staging"));
        let error = Neon {
            http: &http,
            ctx: &ctx,
        }
        .create(&mut Recorded::default())
        .await
        .unwrap_err();
        assert_eq!(
            error.to_string(),
            "Neon parent branch 'staging' not found in project 'app'"
        );
        assert!(server.writes().is_empty());
    }

    #[tokio::test]
    async fn test_existing_branch_gets_no_branch_request_but_missing_parts_are_created() {
        // branch を作った後、role / database を作る前に失敗した deploy の再実行。
        // 親 branch の指定が解決できなくても、branch が在れば見ない
        let server = server(State {
            branch: true,
            ..State::default()
        });
        let http = stub_http(&server);
        let ctx = context(Some("staging"));
        Neon {
            http: &http,
            ctx: &ctx,
        }
        .create(&mut Recorded::default())
        .await
        .unwrap();
        assert_eq!(
            write_lines(&server),
            [format!("POST {ROLES}"), format!("POST {DATABASES}")]
        );
    }

    #[tokio::test]
    async fn test_create_is_a_no_op_when_everything_exists() {
        let server = server(State {
            branch: true,
            role: true,
            database: true,
            endpoint: true,
            ..State::default()
        });
        let http = stub_http(&server);
        let ctx = context(None);
        Neon {
            http: &http,
            ctx: &ctx,
        }
        .create(&mut Recorded::default())
        .await
        .unwrap();
        assert!(server.writes().is_empty(), "{:?}", server.lines());
    }

    #[tokio::test]
    async fn test_create_stops_when_branch_does_not_appear() {
        let server = server(State {
            branch_never_appears: true,
            ..State::default()
        });
        let http = stub_http(&server);
        let ctx = context(None);
        let error = Neon {
            http: &http,
            ctx: &ctx,
        }
        .create(&mut Recorded::default())
        .await
        .unwrap_err();
        assert_eq!(error.to_string(), "branch not found");
        assert_eq!(write_lines(&server), [format!("POST {BRANCHES}")]);
    }

    #[tokio::test]
    async fn test_database_url_quotes_role_and_password() {
        let server = server(State {
            branch: true,
            role: true,
            database: true,
            endpoint: true,
            ..State::default()
        });
        let http = stub_http(&server);
        let ctx = context(None);
        let url = Neon {
            http: &http,
            ctx: &ctx,
        }
        .database_url(&mut Recorded::default())
        .await
        .unwrap();
        // 書き込み用の endpoint を選ぶ (read_only や別 branch の endpoint ではない)
        assert_eq!(
            url.as_deref(),
            Some("postgres://app%20user:p%40ss%2Fw%3Ard@rw.host:5432/appdb?sslmode=require")
        );
        assert!(server.writes().is_empty());
    }

    #[tokio::test]
    async fn test_database_url_is_none_until_ready() {
        // branch が無い / role が無い / 書き込み用の endpoint が無い
        for state in [
            State::default(),
            State {
                branch: true,
                endpoint: true,
                ..State::default()
            },
            State {
                branch: true,
                role: true,
                ..State::default()
            },
        ] {
            let server = server(state);
            let http = stub_http(&server);
            let ctx = context(None);
            let url = Neon {
                http: &http,
                ctx: &ctx,
            }
            .database_url(&mut Recorded::default())
            .await
            .unwrap();
            assert_eq!(url, None, "{:?}", server.lines());
            assert!(server.writes().is_empty());
        }
    }

    /// 削除の要求を受ける stub (branch の一覧は固定)
    fn destroy_server(branches: Json) -> StubServer {
        StubServer::start(move |request: &Request| {
            match (request.method.as_str(), request.path.as_str()) {
                ("GET", "/neon/projects") => {
                    Reply::ok(json!({"projects": [{"id": "p1", "name": "app"}]}))
                }
                ("GET", BRANCHES) => Reply::ok(json!({"branches": branches})),
                ("GET", "/neon/projects/p1/endpoints") => Reply::ok(json!({"endpoints": [
                    {"id": "ep-ro", "branch_id": "br-dev", "type": "read_only"},
                    {"id": "ep-dev", "branch_id": "br-dev", "type": "read_write"},
                    {"id": "ep-main", "branch_id": "br-main", "type": "read_write"},
                ]})),
                ("DELETE", _) => Reply::ok(json!({})),
                _ => Reply::json(500, json!({"message": "unexpected request"})),
            }
        })
    }

    #[test]
    fn test_destroy_plan() {
        let child = json!([
            {"id": "br-main", "name": "main"},
            {"id": "br-dev", "name": "dev", "parent_id": "br-main"},
        ]);
        assert_eq!(destroy_plan(&child, "dev"), Some(DestroyPlan::Branch));
        // root branch で他の branch が同居 → 何も消せない
        assert_eq!(destroy_plan(&child, "main"), Some(DestroyPlan::Blocked));
        let alone = json!([{"id": "br-main", "name": "main"}]);
        assert_eq!(destroy_plan(&alone, "main"), Some(DestroyPlan::Project));
        assert_eq!(destroy_plan(&alone, "dev"), None);
    }

    #[tokio::test]
    async fn test_destroy_deletes_endpoint_then_branch() {
        let server = destroy_server(json!([
            {"id": "br-main", "name": "main"},
            {"id": "br-dev", "name": "dev", "parent_id": "br-main"},
        ]));
        let http = stub_http(&server);
        let ctx = context(None);
        let neon = Neon {
            http: &http,
            ctx: &ctx,
        };
        neon.destroy(&mut Recorded::default()).await.unwrap();
        // 書き込み用の endpoint → branch の順。他の branch と project には触れない
        assert_eq!(
            write_lines(&server),
            [
                "DELETE /neon/projects/p1/endpoints/ep-dev",
                "DELETE /neon/projects/p1/branches/br-dev",
            ]
        );
    }

    #[tokio::test]
    async fn test_destroy_root_branch_deletes_project_only_when_alone() {
        let mut ctx = context(None);
        ctx.branch_name = "main".to_string();
        let server = destroy_server(json!([{"id": "br-main", "name": "main"}]));
        let http = stub_http(&server);
        let neon = Neon {
            http: &http,
            ctx: &ctx,
        };
        neon.destroy(&mut Recorded::default()).await.unwrap();
        assert_eq!(write_lines(&server), ["DELETE /neon/projects/p1"]);

        // 他の branch が同居している root branch: 何も消さずに知らせる
        let server = destroy_server(json!([
            {"id": "br-main", "name": "main"},
            {"id": "br-dev", "name": "dev", "parent_id": "br-main"},
        ]));
        let http = stub_http(&server);
        let neon = Neon {
            http: &http,
            ctx: &ctx,
        };
        let mut sink = Recorded::default();
        neon.destroy(&mut sink).await.unwrap();
        assert!(server.writes().is_empty());
        assert!(sink.0[0].2.starts_with(
            "Neon branch 'main' は root branch のため単体削除できず、project 'app' には"
        ));
    }

    #[tokio::test]
    async fn test_destroy_without_branch_is_a_no_op() {
        let server = destroy_server(json!([{"id": "br-main", "name": "main"}]));
        let http = stub_http(&server);
        let ctx = context(None);
        let neon = Neon {
            http: &http,
            ctx: &ctx,
        };
        neon.destroy(&mut Recorded::default()).await.unwrap();
        assert_eq!(
            neon.destroy_plan(&mut Recorded::default()).await.unwrap(),
            None
        );
        assert!(server.writes().is_empty());
    }

    #[tokio::test]
    async fn test_api_failure_is_an_error() {
        let server = StubServer::start(|request: &Request| match request.path.as_str() {
            "/neon/projects" => Reply::ok(json!({"projects": [{"id": "p1", "name": "app"}]})),
            BRANCHES if request.method == "GET" => Reply::ok(json!({"branches": []})),
            BRANCHES => Reply::json(422, json!({"message": "branch limit exceeded"})),
            _ => Reply::json(401, json!({"message": "unauthorized"})),
        });
        let http = stub_http(&server);
        let ctx = context(None);
        let neon = Neon {
            http: &http,
            ctx: &ctx,
        };
        let error = neon.create(&mut Recorded::default()).await.unwrap_err();
        assert_eq!(error.to_string(), "422: branch limit exceeded");

        // project が無い・認証の失敗は「未準備」ではなく error
        let mut other = context(None);
        other.project_name = "missing".to_string();
        let neon = Neon {
            http: &http,
            ctx: &other,
        };
        let error = neon
            .database_url(&mut Recorded::default())
            .await
            .unwrap_err();
        assert_eq!(error.to_string(), "Neon project 'missing' not found");
    }
}
