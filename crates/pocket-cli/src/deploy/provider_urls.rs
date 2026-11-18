//! 管理 API から算出する接続 URL (computed mode。deprecated) の実体と、deploy が
//! provider を作る対象の選び方。
//!
//! `provisioning = "command"` の DB / KVS は deploy が作らない ([`deploy_managed`])。
//! computed な managed secret の URL の算出は Python 版と同じく provisioning を見ない
//! (computed の宣言があれば、どちらの方式でも管理 API を呼ぶ)

use anyhow::Result;
use magic_pocket_settings::context::Context;
use magic_pocket_settings::model::ProvisioningMode;

use super::mediator::DatabaseUrls;
use super::neon::Neon;
use super::tidb::{MysqlSql, TiDb};
use super::upstash::Upstash;
use crate::status::providers::Http;
use crate::status::sink::Terminal;

/// deploy が作る provider (Python 版 `get_resources` の条件)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Provider {
    Neon,
    Tidb,
    Upstash,
}

/// deploy が作る provider の一覧。宣言が無いものと `provisioning = "command"` のものは
/// 入らない (deploy は触れず、資格情報も要らない)
pub fn deploy_managed(ctx: &Context) -> Vec<Provider> {
    let deploy = |mode: Option<ProvisioningMode>| mode == Some(ProvisioningMode::Deploy);
    let mut providers = Vec::new();
    if deploy(ctx.neon.as_ref().map(|neon| neon.provisioning)) {
        providers.push(Provider::Neon);
    }
    if deploy(ctx.tidb.as_ref().map(|tidb| tidb.provisioning)) {
        providers.push(Provider::Tidb);
    }
    if deploy(ctx.upstash.as_ref().map(|upstash| upstash.provisioning)) {
        providers.push(Provider::Upstash);
    }
    providers
}

/// provider の API に繋ぐ [`DatabaseUrls`]
pub struct ProviderUrls {
    pub http: Http,
}

impl ProviderUrls {
    pub fn new() -> Result<Self> {
        Ok(Self { http: Http::new()? })
    }
}

impl DatabaseUrls for ProviderUrls {
    async fn neon_database_url(&self, ctx: &Context) -> Result<Option<String>> {
        let Some(neon) = &ctx.neon else {
            return Ok(None);
        };
        let http = &self.http;
        Neon { http, ctx: neon }
            .database_url(&mut Terminal::new())
            .await
    }

    async fn tidb_database_url(&self, ctx: &Context) -> Result<Option<String>> {
        let Some(tidb) = &ctx.tidb else {
            return Ok(None);
        };
        TiDb::new(&self.http, tidb, &MysqlSql).database_url().await
    }

    async fn upstash_redis_url(&self, ctx: &Context) -> Result<Option<String>> {
        let Some(upstash) = &ctx.upstash else {
            return Ok(None);
        };
        let http = &self.http;
        Upstash { http, ctx: upstash }.redis_url().await
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;
    use crate::deploy::mediator::Mediator;
    use crate::deploy::testing::{stub_http, FakeStore, Reply, Request, StubServer};
    use crate::stack::testing::context_from_toml;
    use crate::status::sink::Recorded;

    fn toml(databases: &str, managed: &str) -> String {
        format!(
            r#"
[general]
region = "ap-southeast-1"
project_name = "testprj"
stages = ["prod"]

{databases}

[container.web]
dockerfile_path = "Dockerfile"

[container.web.secrets.managed]
{managed}
"#
        )
    }

    const WEB: &str = "prod-testprj-web-pocket";
    const ALL: &str = "[neon]\nproject_name = \"p\"\n\n[tidb]\n\n[upstash]\n";
    const ALL_BY_COMMAND: &str = "[neon]\nproject_name = \"p\"\nprovisioning = \"command\"\n\n\
                                  [tidb]\nprovisioning = \"command\"\n\n\
                                  [upstash]\nprovisioning = \"command\"\n";

    fn env(ctx: &mut Context) {
        // 資格情報は .env から入る。テストでは Context に直接入れる
        if let Some(neon) = ctx.neon.as_mut() {
            neon.api_key = Some("neon-key".to_string());
        }
        if let Some(upstash) = ctx.upstash.as_mut() {
            upstash.email = Some("ops@example.com".to_string());
            upstash.api_key = Some("upstash-key".to_string());
            upstash.database_name = "prod-testprj".to_string();
        }
    }

    #[test]
    fn test_deploy_managed_skips_command_provisioning() {
        let ctx = context_from_toml(&toml(ALL, ""), "prod");
        assert_eq!(
            deploy_managed(&ctx),
            [Provider::Neon, Provider::Tidb, Provider::Upstash]
        );
        // command: deploy は provider を作らない
        let ctx = context_from_toml(&toml(ALL_BY_COMMAND, ""), "prod");
        assert_eq!(deploy_managed(&ctx), []);
        let ctx = context_from_toml(&toml("", ""), "prod");
        assert_eq!(deploy_managed(&ctx), []);
    }

    /// Neon (branch main に role と database が在る) と Upstash (database が在る) の stub
    fn server(upstash_state: &'static str) -> StubServer {
        StubServer::start(move |request: &Request| {
            let branch = "/neon/projects/p1/branches/br-main";
            let path = request.path.as_str();
            if request.method != "GET" {
                return Reply::json(500, json!({"message": "書き込みは来ないはず"}));
            }
            if path == "/neon/projects" {
                Reply::ok(json!({"projects": [{"id": "p1", "name": "p"}]}))
            } else if path == "/neon/projects/p1/branches" {
                Reply::ok(json!({"branches": [{"id": "br-main", "name": "main"}]}))
            } else if path == format!("{branch}/roles/testprj") {
                Reply::ok(json!({"role": {"name": "testprj"}}))
            } else if path == format!("{branch}/roles/testprj/reveal_password") {
                Reply::ok(json!({"password": "pw"}))
            } else if path == "/neon/projects/p1/endpoints" {
                Reply::ok(json!({"endpoints": [
                    {"branch_id": "br-main", "type": "read_write", "host": "ep.example"}
                ]}))
            } else if path == "/upstash/databases" {
                Reply::ok(json!([{
                    "database_name": "prod-testprj",
                    "endpoint": "redis.example",
                    "port": 6379,
                    "password": "rp",
                    "tls": true,
                    "state": upstash_state,
                }]))
            } else {
                Reply::json(404, json!({"message": format!("unexpected {path}")}))
            }
        })
    }

    async fn ensure(ctx: &Context, server: &StubServer) -> (Result<()>, FakeStore, Recorded) {
        let store = FakeStore::default();
        let urls = ProviderUrls {
            http: stub_http(server),
        };
        let mut sink = Recorded::default();
        let mediator = Mediator {
            ctx,
            store: &store,
            urls: &urls,
        };
        let result = mediator
            .ensure_pocket_managed_secrets(&mut sink, || Ok(ctx.clone()))
            .await
            .map(|_| ());
        (result, store, sink)
    }

    const COMPUTED: &str = "DATABASE_URL = { type = \"neon_database_url\" }\n\
                            REDIS_URL = { type = \"upstash_redis_url\" }";

    #[tokio::test]
    async fn test_computed_secrets_are_generated_from_the_provider_api() {
        let mut ctx = context_from_toml(&toml(ALL, COMPUTED), "prod");
        env(&mut ctx);
        let neon = ctx.neon.clone().unwrap();
        assert_eq!(
            (neon.branch_name.as_str(), neon.role_name.as_str()),
            ("main", "testprj")
        );
        let server = server("active");
        let (result, store, _) = ensure(&ctx, &server).await;
        result.unwrap();
        let secrets = store.secrets(WEB);
        assert_eq!(
            secrets["DATABASE_URL"],
            format!(
                "postgres://testprj:pw@ep.example:5432/{}?sslmode=require",
                neon.name
            )
        );
        assert_eq!(
            secrets["REDIS_URL"],
            "rediss://default:rp@redis.example:6379"
        );
        assert!(server.writes().is_empty());
    }

    #[tokio::test]
    async fn test_computed_url_ignores_provisioning_mode() {
        // Python 版と同じ: computed の宣言があれば、provisioning = "command" でも
        // 管理 API から URL を算出する (deploy が provider を作らないこととは別)
        let mut ctx = context_from_toml(&toml(ALL_BY_COMMAND, COMPUTED), "prod");
        env(&mut ctx);
        assert_eq!(deploy_managed(&ctx), []);
        let server = server("active");
        let (result, store, _) = ensure(&ctx, &server).await;
        result.unwrap();
        assert_eq!(
            store.secrets(WEB)["REDIS_URL"],
            "rediss://default:rp@redis.example:6379"
        );
    }

    #[tokio::test]
    async fn test_stored_mode_never_calls_the_provider() {
        // command + stored の user secret: managed に computed が無ければ API を呼ばない
        let managed = "SECRET_KEY = { type = \"password\" }";
        let mut ctx = context_from_toml(&toml(ALL_BY_COMMAND, managed), "prod");
        env(&mut ctx);
        let server = server("active");
        let (result, _, _) = ensure(&ctx, &server).await;
        result.unwrap();
        assert!(server.requests().is_empty(), "{:?}", server.lines());
    }

    #[tokio::test]
    async fn test_not_ready_provider_fails_like_python() {
        let mut ctx = context_from_toml(&toml(ALL, COMPUTED), "prod");
        env(&mut ctx);
        let server = server("deleted");
        let (result, store, sink) = ensure(&ctx, &server).await;
        assert_eq!(
            result.unwrap_err().to_string(),
            "Secret generation for REDIS_URL is failed."
        );
        assert!(sink
            .0
            .iter()
            .any(|(_, _, text)| text == "upstash redis is not ready"));
        assert!(store.calls.borrow().is_empty());
    }
}
