//! TiDB Cloud (serverless) の cluster と database の確保、接続 URL (Python 版
//! `resources/tidb.py` の `create` / `database_url`)。
//!
//! root password は API から読めない。cluster を作った処理の中では生成した値を使い回し、
//! 手元に無いとき (既存の cluster) は再設定してから使う。再設定は以前の password を
//! 無効にするので、URL の算出のたびに再設定になる点も Python 版と同じ。
//! 削除は destroy と個別コマンドの移植で扱う

use std::cell::RefCell;
use std::fmt;

use anyhow::{bail, Result};
use magic_pocket_settings::context::TiDbContext;
use reqwest::Method;
use serde_json::{json, Value as Json};

use super::generate;
use crate::stack::ops::WaitOptions;
use crate::status::providers::{id_text, json_list, tidb_select_project, Auth, Http};

/// まだ使えない状態 (Python 版の `TiDbResourceIsNotReady`)。
///
/// 手順の中では error として止まり、接続 URL の算出 ([`TiDb::database_url`]) では
/// 「未準備」(`None`) になる
#[derive(Debug)]
pub struct NotReady(pub String);

impl fmt::Display for NotReady {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for NotReady {}

fn not_ready<T>(message: &str) -> Result<T> {
    Err(NotReady(message.to_string()).into())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Cluster {
    pub id: String,
    /// `ACTIVE` / `CREATING` など
    pub status: String,
    pub host: String,
    pub port: u16,
    /// `<prefix>.root`
    pub user: String,
}

/// 一覧の 1 件から、名前が一致する cluster を取り出す
pub fn find_cluster(tidb: &TiDbContext, clusters: &[Json]) -> Option<Cluster> {
    let c = clusters
        .iter()
        .find(|c| c["displayName"].as_str() == Some(tidb.cluster_name.as_str()))?;
    let public = &c["endpoints"]["public"];
    let port = match &public["port"] {
        Json::String(text) => text.parse().ok(),
        value => value.as_u64().and_then(|port| u16::try_from(port).ok()),
    };
    Some(Cluster {
        id: id_text(&c["clusterId"]),
        status: c["state"].as_str().unwrap_or("UNKNOWN").to_string(),
        host: public["host"].as_str().unwrap_or_default().to_string(),
        port: port.unwrap_or(4000),
        user: format!("{}.root", c["userPrefix"].as_str().unwrap_or_default()),
    })
}

/// cluster 作成の body。接続元は制限しない (Lambda の出口の IP が決まらないため)
pub fn cluster_body(tidb: &TiDbContext, project_id: &str, root_password: &str) -> Json {
    json!({
        "displayName": tidb.cluster_name,
        "region": {"name": format!("regions/aws-{}", tidb.region)},
        "rootPassword": root_password,
        "labels": {"tidb.cloud/project": project_id},
        "endpoints": {
            "public": {
                "authorizedNetworks": [
                    {
                        "startIpAddress": "0.0.0.0",
                        "endIpAddress": "255.255.255.255",
                        "displayName": "Allow all",
                    }
                ]
            }
        },
    })
}

/// SQL の接続先
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SqlTarget {
    pub host: String,
    pub port: u16,
    pub user: String,
    pub password: String,
}

/// cluster への SQL の実行 (MySQL protocol・TLS)
pub trait Sql {
    async fn execute(&self, target: &SqlTarget, statement: &str) -> Result<()>;
}

/// 実際に接続する [`Sql`]
pub struct MysqlSql;

impl Sql for MysqlSql {
    async fn execute(&self, target: &SqlTarget, statement: &str) -> Result<()> {
        use mysql_async::prelude::Queryable as _;

        let options = mysql_async::OptsBuilder::default()
            .ip_or_hostname(target.host.clone())
            .tcp_port(target.port)
            .user(Some(target.user.clone()))
            .pass(Some(target.password.clone()))
            .ssl_opts(Some(mysql_async::SslOpts::default()));
        let mut connection = mysql_async::Conn::new(options).await?;
        let result = connection.query_drop(statement).await;
        connection.disconnect().await?;
        Ok(result?)
    }
}

/// cluster が ACTIVE になるまでの待ち (Python 版は 5 秒おきに 60 回)
pub fn default_wait() -> WaitOptions {
    WaitOptions::new(300, 5)
}

pub struct TiDb<'a, Q> {
    pub http: &'a Http,
    pub ctx: &'a TiDbContext,
    pub sql: &'a Q,
    pub wait: WaitOptions,
    /// この処理の中で設定した root password
    root_password: RefCell<Option<String>>,
}

impl<'a, Q: Sql> TiDb<'a, Q> {
    pub fn new(http: &'a Http, ctx: &'a TiDbContext, sql: &'a Q) -> Self {
        Self {
            http,
            ctx,
            sql,
            wait: default_wait(),
            root_password: RefCell::new(None),
        }
    }

    fn keys(&self) -> Result<(&str, &str)> {
        let key = |value: &'a Option<String>| value.as_deref().filter(|v| !v.is_empty());
        match (key(&self.ctx.public_key), key(&self.ctx.private_key)) {
            (Some(public), Some(private)) => Ok((public, private)),
            _ => not_ready(
                "TiDB API keys not configured. \
                 Set tidb_public_key and tidb_private_key in .env",
            ),
        }
    }

    async fn request(&self, method: Method, url: &str, body: Option<&Json>) -> Result<Json> {
        let (public, private) = self.keys()?;
        let write = method != Method::GET;
        let response = self
            .http
            .request(method, url, Auth::Digest(public, private), body)
            .await?;
        if !response.ok() {
            bail!("{}: {}", response.status, response.body);
        }
        if write {
            self.http.settle().await;
        }
        response.json()
    }

    /// 使う project の id。project が 1 つも無い・名前が一致しないなら `None`
    async fn project_id(&self) -> Result<Option<String>> {
        let url = format!("{}projects", self.http.endpoints.tidb_iam);
        let projects = self.request(Method::GET, &url, None).await?;
        match tidb_select_project(self.ctx, json_list(&projects["projects"])) {
            Ok(project) => Ok(project.map(|project| id_text(&project["id"]))),
            // project が複数あって選べない
            Err(error) => Err(NotReady(error.to_string()).into()),
        }
    }

    async fn cluster(&self, project_id: &str) -> Result<Option<Cluster>> {
        let url = format!(
            "{}clusters?filter=projectId={project_id}",
            self.http.endpoints.tidb_serverless
        );
        let clusters = self.request(Method::GET, &url, None).await?;
        Ok(find_cluster(self.ctx, json_list(&clusters["clusters"])))
    }

    /// root password を作り直して設定する (以前の password は使えなくなる)
    async fn reset_password(&self, cluster: &Cluster) -> Result<String> {
        let password = generate::alphanumeric(24)?;
        let url = format!(
            "{}clusters/{}/password",
            self.http.endpoints.tidb_serverless, cluster.id
        );
        self.request(Method::PUT, &url, Some(&json!({"password": password})))
            .await?;
        *self.root_password.borrow_mut() = Some(password.clone());
        Ok(password)
    }

    /// この処理の中で設定した password。無ければ再設定する
    async fn password(&self, cluster: &Cluster) -> Result<String> {
        let known = self.root_password.borrow().clone();
        match known {
            Some(password) => Ok(password),
            None => self.reset_password(cluster).await,
        }
    }

    /// cluster (無ければ作って ACTIVE を待つ) と database を確保する
    pub async fn create(&self) -> Result<()> {
        let project_id = self.project_id().await?;
        let existing = match &project_id {
            Some(project_id) => self.cluster(project_id).await?,
            None => None,
        };
        let cluster = match existing {
            Some(cluster) => cluster,
            None => {
                let Some(project_id) = project_id else {
                    return not_ready(
                        "No TiDB Cloud project found. Create one at https://tidbcloud.com/",
                    );
                };
                self.create_cluster(&project_id).await?
            }
        };
        let target = SqlTarget {
            host: cluster.host.clone(),
            port: cluster.port,
            user: cluster.user.clone(),
            password: self.password(&cluster).await?,
        };
        let statement = format!("CREATE DATABASE IF NOT EXISTS `{}`", self.ctx.database_name);
        self.sql.execute(&target, &statement).await
    }

    async fn create_cluster(&self, project_id: &str) -> Result<Cluster> {
        let password = generate::alphanumeric(24)?;
        let url = format!("{}clusters", self.http.endpoints.tidb_serverless);
        let body = cluster_body(self.ctx, project_id, &password);
        self.request(Method::POST, &url, Some(&body)).await?;
        *self.root_password.borrow_mut() = Some(password);
        for _ in 0..self.wait.attempts {
            if let Some(cluster) = self.cluster(project_id).await? {
                if cluster.status == "ACTIVE" {
                    return Ok(cluster);
                }
            }
            tokio::time::sleep(self.wait.interval).await;
        }
        not_ready("Cluster did not become available in time")
    }

    /// 接続 URL。資格情報が無い・cluster が無い・ACTIVE でないなら `None` (未準備)。
    /// API の失敗は error
    pub async fn database_url(&self) -> Result<Option<String>> {
        match self.ready_database_url().await {
            Ok(url) => Ok(Some(url)),
            Err(error) if error.downcast_ref::<NotReady>().is_some() => Ok(None),
            Err(error) => Err(error),
        }
    }

    async fn ready_database_url(&self) -> Result<String> {
        let cluster = match self.project_id().await? {
            Some(project_id) => self.cluster(&project_id).await?,
            None => None,
        };
        let Some(cluster) = cluster else {
            return not_ready("Cluster not found");
        };
        if cluster.status != "ACTIVE" {
            return not_ready("Cluster is not available");
        }
        let password = self.password(&cluster).await?;
        Ok(format!(
            "mysql://{}:{password}@{}:{}/{}",
            cluster.user, cluster.host, cluster.port, self.ctx.database_name
        ))
    }
}

#[cfg(test)]
mod tests {
    use std::sync::{Arc, Mutex};

    use magic_pocket_settings::model::ProvisioningMode;

    use super::*;
    use crate::deploy::poll::testing::fast;
    use crate::deploy::testing::{stub_http, Reply, Request, StubServer};

    const CHALLENGE: &str = "Digest realm=\"tidb.cloud\", domain=\"\", \
                             nonce=\"e1b9494e87347ade5366edcb24423951\", algorithm=MD5, \
                             qop=\"auth\", stale=false";
    const CLUSTERS: &str = "/serverless/clusters";
    const LIST: &str = "/serverless/clusters?filter=projectId=1";
    const PASSWORD: &str = "/serverless/clusters/c-1/password";

    fn context() -> TiDbContext {
        TiDbContext {
            public_key: Some("pub".to_string()),
            private_key: Some("priv".to_string()),
            tidb_project: None,
            cluster_name: "dev-app".to_string(),
            database_name: "appdb".to_string(),
            region: "ap-northeast-1".to_string(),
            project_name: "app".to_string(),
            provisioning: ProvisioningMode::Deploy,
        }
    }

    #[derive(Default)]
    struct FakeSql {
        calls: RefCell<Vec<(SqlTarget, String)>>,
    }

    impl Sql for FakeSql {
        async fn execute(&self, target: &SqlTarget, statement: &str) -> Result<()> {
            self.calls
                .borrow_mut()
                .push((target.clone(), statement.to_string()));
            Ok(())
        }
    }

    fn cluster(state: &str) -> Json {
        json!({
            "clusterId": "c-1",
            "displayName": "dev-app",
            "state": state,
            "userPrefix": "3xAmPl3",
            "endpoints": {"public": {"host": "gateway.tidbcloud.example", "port": 4000}},
        })
    }

    /// TiDB Cloud の API の状態
    #[derive(Clone)]
    struct State {
        projects: Json,
        /// 今の cluster の state (`None` は cluster が無い)
        cluster: Option<String>,
        /// 作成の後、一覧を何回見たら ACTIVE になるか (`None` はならない)
        active_after: Option<u32>,
        polls: u32,
    }

    impl State {
        fn new(cluster: Option<&str>) -> Self {
            Self {
                projects: json!([{"id": 1, "name": "default"}]),
                cluster: cluster.map(str::to_string),
                active_after: Some(2),
                polls: 0,
            }
        }
    }

    /// Digest 認証を求める stub: 認証なしの要求には 401 と challenge を返す
    fn server(initial: State) -> StubServer {
        let state = Arc::new(Mutex::new(initial));
        StubServer::start(move |request: &Request| {
            if request.header("authorization").is_none() {
                return Reply::json(401, json!({"message": "unauthorized"}))
                    .header("WWW-Authenticate", CHALLENGE);
            }
            let mut state = state.lock().unwrap();
            match (request.method.as_str(), request.path.as_str()) {
                ("GET", "/iam/projects") => Reply::ok(json!({"projects": state.projects})),
                ("GET", LIST) => {
                    if state.cluster.as_deref() == Some("CREATING") {
                        state.polls += 1;
                        if state.active_after.is_some_and(|after| state.polls >= after) {
                            state.cluster = Some("ACTIVE".to_string());
                        }
                    }
                    let clusters: Vec<Json> = state.cluster.iter().map(|s| cluster(s)).collect();
                    Reply::ok(json!({"clusters": clusters}))
                }
                ("POST", CLUSTERS) => {
                    state.cluster = Some("CREATING".to_string());
                    Reply::ok(json!({"clusterId": "c-1"}))
                }
                ("PUT", PASSWORD) => Reply::ok(json!({})),
                _ => Reply::json(500, json!({"message": "unexpected request"})),
            }
        })
    }

    /// 認証付きで届いた書き込み
    fn authed_writes(server: &StubServer) -> Vec<Request> {
        server
            .writes()
            .into_iter()
            .filter(|request| request.header("authorization").is_some())
            .collect()
    }

    fn tidb<'a>(http: &'a Http, ctx: &'a TiDbContext, sql: &'a FakeSql) -> TiDb<'a, FakeSql> {
        let mut tidb = TiDb::new(http, ctx, sql);
        tidb.wait = fast(5);
        tidb
    }

    #[test]
    fn test_find_cluster_reads_endpoint_and_user() {
        let ctx = context();
        let found = find_cluster(&ctx, &[cluster("ACTIVE")]).unwrap();
        assert_eq!(
            found,
            Cluster {
                id: "c-1".to_string(),
                status: "ACTIVE".to_string(),
                host: "gateway.tidbcloud.example".to_string(),
                port: 4000,
                user: "3xAmPl3.root".to_string(),
            }
        );
        // endpoint がまだ無い cluster (作成直後)
        let bare = json!({"clusterId": 7, "displayName": "dev-app"});
        let found = find_cluster(&ctx, &[bare]).unwrap();
        assert_eq!(
            (found.id.as_str(), found.status.as_str(), found.port),
            ("7", "UNKNOWN", 4000)
        );
        assert_eq!(find_cluster(&ctx, &[]), None);
    }

    #[tokio::test]
    async fn test_create_makes_cluster_waits_and_creates_database() {
        let server = server(State::new(None));
        let http = stub_http(&server);
        let (ctx, sql) = (context(), FakeSql::default());
        tidb(&http, &ctx, &sql).create().await.unwrap();

        let writes = authed_writes(&server);
        assert_eq!(writes.len(), 1, "{:?}", server.lines());
        assert_eq!(writes[0].line(), format!("POST {CLUSTERS}"));
        let body = writes[0].json();
        let root_password = body["rootPassword"].as_str().unwrap().to_string();
        assert_eq!(root_password.len(), 24);
        assert!(root_password.chars().all(|c| c.is_ascii_alphanumeric()));
        assert_eq!(body, cluster_body(&ctx, "1", &root_password));
        assert_eq!(body["region"]["name"], "regions/aws-ap-northeast-1");
        assert_eq!(body["labels"]["tidb.cloud/project"], "1");

        // cluster を作った処理の中では、生成した password を使い回す (再設定しない)
        let calls = sql.calls.borrow();
        assert_eq!(calls.len(), 1);
        assert_eq!(
            calls[0].0,
            SqlTarget {
                host: "gateway.tidbcloud.example".to_string(),
                port: 4000,
                user: "3xAmPl3.root".to_string(),
                password: root_password,
            }
        );
        assert_eq!(calls[0].1, "CREATE DATABASE IF NOT EXISTS `appdb`");
    }

    #[tokio::test]
    async fn test_digest_challenge_is_answered_with_the_same_body() {
        let server = server(State::new(None));
        let http = stub_http(&server);
        let (ctx, sql) = (context(), FakeSql::default());
        tidb(&http, &ctx, &sql).create().await.unwrap();

        // 書き込みは 401 の challenge → 認証付きの再送 の 2 回届き、body は同じ
        let posts: Vec<Request> = server
            .requests()
            .into_iter()
            .filter(|request| request.method == "POST")
            .collect();
        assert_eq!(posts.len(), 2);
        assert_eq!(posts[0].header("authorization"), None);
        assert_eq!(posts[0].json(), posts[1].json());
        assert_eq!(posts[1].header("content-type"), Some("application/json"));
        // 応答は method と path (query 込み) から計算する
        let authorization = posts[1].header("authorization").unwrap();
        assert!(
            authorization.starts_with("Digest username=\"pub\", realm=\"tidb.cloud\""),
            "{authorization}"
        );
        assert!(authorization.contains("uri=\"/serverless/clusters\""));
        let cnonce = authorization
            .split("cnonce=\"")
            .nth(1)
            .and_then(|rest| rest.split('"').next())
            .unwrap();
        let want = crate::status::providers::digest_authorization(
            CHALLENGE,
            "pub",
            "priv",
            "POST",
            "/serverless/clusters",
            cnonce,
        )
        .unwrap();
        assert_eq!(authorization, want);
        let list = server
            .requests()
            .into_iter()
            .find(|r| r.path == LIST && r.header("authorization").is_some())
            .unwrap();
        assert!(list
            .header("authorization")
            .unwrap()
            .contains("uri=\"/serverless/clusters?filter=projectId=1\""));
    }

    #[tokio::test]
    async fn test_create_on_existing_cluster_resets_password_once() {
        let server = server(State::new(Some("ACTIVE")));
        let http = stub_http(&server);
        let (ctx, sql) = (context(), FakeSql::default());
        let tidb = tidb(&http, &ctx, &sql);
        tidb.create().await.unwrap();
        // 同じ処理の中の URL は、再設定した password をそのまま使う
        let first = tidb.database_url().await.unwrap().unwrap();
        let second = tidb.database_url().await.unwrap().unwrap();

        let writes = authed_writes(&server);
        assert_eq!(writes.len(), 1, "{:?}", server.lines());
        assert_eq!(writes[0].line(), format!("PUT {PASSWORD}"));
        let password = writes[0].json()["password"].as_str().unwrap().to_string();
        assert_eq!(password.len(), 24);
        assert_eq!(sql.calls.borrow()[0].0.password, password);
        assert_eq!(
            first,
            format!("mysql://3xAmPl3.root:{password}@gateway.tidbcloud.example:4000/appdb")
        );
        assert_eq!(first, second);
    }

    #[tokio::test]
    async fn test_database_url_in_a_new_process_resets_password() {
        // mediator は URL の算出のたびに新しく作るので、手元に password が無い
        let server = server(State::new(Some("ACTIVE")));
        let http = stub_http(&server);
        let (ctx, sql) = (context(), FakeSql::default());
        let url = tidb(&http, &ctx, &sql).database_url().await.unwrap();
        let writes = authed_writes(&server);
        assert_eq!(writes.len(), 1);
        assert_eq!(writes[0].line(), format!("PUT {PASSWORD}"));
        let password = writes[0].json()["password"].as_str().unwrap().to_string();
        assert_eq!(
            url.unwrap(),
            format!("mysql://3xAmPl3.root:{password}@gateway.tidbcloud.example:4000/appdb")
        );
        assert!(sql.calls.borrow().is_empty());
    }

    #[tokio::test]
    async fn test_database_url_is_none_until_ready() {
        // cluster が無い・ACTIVE でない → 未準備 (password も再設定しない)
        for cluster in [None, Some("CREATING")] {
            let mut state = State::new(cluster);
            state.active_after = None;
            let server = server(state);
            let http = stub_http(&server);
            let (ctx, sql) = (context(), FakeSql::default());
            let url = tidb(&http, &ctx, &sql).database_url().await.unwrap();
            assert_eq!(url, None);
            assert!(authed_writes(&server).is_empty());
        }
        // 資格情報が無い → API を呼ばずに未準備
        let server = server(State::new(Some("ACTIVE")));
        let http = stub_http(&server);
        let (mut ctx, sql) = (context(), FakeSql::default());
        ctx.private_key = None;
        let url = tidb(&http, &ctx, &sql).database_url().await.unwrap();
        assert_eq!(url, None);
        assert!(server.requests().is_empty());
        // project が複数あって選べない → 未準備
        let mut state = State::new(Some("ACTIVE"));
        state.projects = json!([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]);
        let server = self::server(state);
        let http = stub_http(&server);
        let (ctx, sql) = (context(), FakeSql::default());
        let url = tidb(&http, &ctx, &sql).database_url().await.unwrap();
        assert_eq!(url, None);
    }

    #[tokio::test]
    async fn test_create_times_out_when_cluster_never_becomes_active() {
        let mut state = State::new(None);
        state.active_after = None;
        let server = server(state);
        let http = stub_http(&server);
        let (ctx, sql) = (context(), FakeSql::default());
        let error = tidb(&http, &ctx, &sql).create().await.unwrap_err();
        assert_eq!(
            error.to_string(),
            "Cluster did not become available in time"
        );
        // 待ちの回数だけ一覧を見て、database は作らない
        let polls = server
            .requests()
            .iter()
            .filter(|r| r.path == LIST && r.header("authorization").is_some())
            .count();
        assert_eq!(polls, 1 + 5);
        assert!(sql.calls.borrow().is_empty());
    }

    #[tokio::test]
    async fn test_create_errors() {
        // project が無い
        let mut state = State::new(None);
        state.projects = json!([]);
        let server = server(state);
        let http = stub_http(&server);
        let (ctx, sql) = (context(), FakeSql::default());
        let error = tidb(&http, &ctx, &sql).create().await.unwrap_err();
        assert_eq!(
            error.to_string(),
            "No TiDB Cloud project found. Create one at https://tidbcloud.com/"
        );
        assert!(authed_writes(&server).is_empty());

        // 資格情報が無い
        let (mut ctx, sql) = (context(), FakeSql::default());
        ctx.public_key = None;
        let error = tidb(&http, &ctx, &sql).create().await.unwrap_err();
        assert_eq!(
            error.to_string(),
            "TiDB API keys not configured. Set tidb_public_key and tidb_private_key in .env"
        );

        // API の失敗 (認証しても 4xx) は、URL の算出でも error のまま
        let server = StubServer::start(|request: &Request| {
            if request.header("authorization").is_none() {
                return Reply::json(401, json!({})).header("WWW-Authenticate", CHALLENGE);
            }
            Reply::json(403, json!({"message": "forbidden"}))
        });
        let http = stub_http(&server);
        let (ctx, sql) = (context(), FakeSql::default());
        let error = tidb(&http, &ctx, &sql).database_url().await.unwrap_err();
        assert_eq!(error.to_string(), "403: {\"message\":\"forbidden\"}");
    }
}
