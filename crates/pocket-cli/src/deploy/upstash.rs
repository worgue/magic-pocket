//! Upstash Redis の database の確保と接続 URL (Python 版 `resources/upstash.py` の
//! `create` / `redis_url`)。削除は destroy と個別コマンドの移植で扱う

use anyhow::{bail, Result};
use magic_pocket_settings::context::UpstashContext;
use reqwest::Method;
use serde_json::{json, Value as Json};

use crate::status::providers::{json_list, Auth, Http};
use crate::status::sink::Sink;

/// database 作成の body (region と plan は固定)
pub fn create_body(upstash: &UpstashContext) -> Json {
    json!({
        "database_name": upstash.database_name,
        "platform": "aws",
        "primary_region": "ap-southeast-1",
        "plan": "payg",
        "budget": upstash.budget,
        "tls": true,
        "eviction": true,
    })
}

/// 接続 URL。database が active でなければ `None` (未準備)
pub fn redis_url(database: &Json) -> Option<String> {
    if database["state"].as_str() != Some("active") {
        return None;
    }
    let protocol = if database["tls"].as_bool() == Some(true) {
        "rediss"
    } else {
        "redis"
    };
    Some(format!(
        "{protocol}://default:{}@{}:{}",
        database["password"].as_str().unwrap_or_default(),
        database["endpoint"].as_str().unwrap_or_default(),
        database["port"].as_i64().unwrap_or_default()
    ))
}

pub struct Upstash<'a> {
    pub http: &'a Http,
    pub ctx: &'a UpstashContext,
}

impl Upstash<'_> {
    /// (email, API key)。どちらかが無ければ `None`
    fn credentials(&self) -> Option<(&str, &str)> {
        let email = self.ctx.email.as_deref().filter(|v| !v.is_empty())?;
        let api_key = self.ctx.api_key.as_deref().filter(|v| !v.is_empty())?;
        Some((email, api_key))
    }

    async fn request(
        &self,
        method: Method,
        path: &str,
        auth: (&str, &str),
        body: Option<&Json>,
    ) -> Result<Json> {
        let url = format!("{}/{path}", self.http.endpoints.upstash);
        let response = self
            .http
            .request(method, &url, Auth::Basic(auth.0, auth.1), body)
            .await?;
        if !response.ok() {
            bail!("Upstash API error: {} {}", response.status, response.body);
        }
        response.json()
    }

    /// 名前が一致する database (一覧の先頭のページから探す)
    async fn database(&self, auth: (&str, &str)) -> Result<Option<Json>> {
        let databases = self.request(Method::GET, "databases", auth, None).await?;
        Ok(json_list(&databases)
            .iter()
            .find(|db| db["database_name"].as_str() == Some(self.ctx.database_name.as_str()))
            .cloned())
    }

    /// database が無ければ作る
    pub async fn create(&self, sink: &mut dyn Sink) -> Result<()> {
        let Some(auth) = self.credentials() else {
            bail!("UPSTASH_EMAIL と UPSTASH_API_KEY を設定してください");
        };
        let name = &self.ctx.database_name;
        if self.database(auth).await?.is_some() {
            sink.info(&format!("Upstash データベース '{name}' は既に存在します"));
            return Ok(());
        }
        sink.log(&format!("Upstash データベースを作成します: {name}"));
        let body = create_body(self.ctx);
        self.request(Method::POST, "database", auth, Some(&body))
            .await?;
        sink.success(&format!("Upstash データベースを作成しました: {name}"));
        Ok(())
    }

    /// 接続 URL。資格情報が無い・database が無い・active でないなら `None` (未準備)
    pub async fn redis_url(&self) -> Result<Option<String>> {
        let Some(auth) = self.credentials() else {
            return Ok(None);
        };
        Ok(self
            .database(auth)
            .await?
            .and_then(|database| redis_url(&database)))
    }
}

#[cfg(test)]
mod tests {
    use magic_pocket_settings::model::ProvisioningMode;

    use super::*;
    use crate::deploy::testing::{stub_http, Reply, Request, StubServer};
    use crate::status::sink::{Recorded, Style};

    fn context() -> UpstashContext {
        UpstashContext {
            email: Some("ops@example.com".to_string()),
            api_key: Some("upstash-key".to_string()),
            database_name: "dev-app".to_string(),
            budget: 20,
            provisioning: ProvisioningMode::Deploy,
        }
    }

    fn database(name: &str, state: &str, tls: bool) -> Json {
        json!({
            "database_id": "db-1",
            "database_name": name,
            "endpoint": "apn1-x.upstash.io",
            "port": 6379,
            "password": "secret",
            "tls": tls,
            "state": state,
        })
    }

    fn server(databases: Json) -> StubServer {
        StubServer::start(move |request: &Request| {
            match (request.method.as_str(), request.path.as_str()) {
                ("GET", "/upstash/databases") => Reply::ok(databases.clone()),
                ("POST", "/upstash/database") => Reply::ok(json!({"database_id": "db-new"})),
                _ => Reply::json(500, json!({"error": "unexpected request"})),
            }
        })
    }

    #[tokio::test]
    async fn test_create_posts_the_database() {
        let server = server(json!([database("other", "active", true)]));
        let http = stub_http(&server);
        let ctx = context();
        let mut sink = Recorded::default();
        Upstash {
            http: &http,
            ctx: &ctx,
        }
        .create(&mut sink)
        .await
        .unwrap();
        let writes = server.writes();
        assert_eq!(writes.len(), 1);
        assert_eq!(writes[0].line(), "POST /upstash/database");
        assert_eq!(
            writes[0].json(),
            json!({
                "database_name": "dev-app",
                "platform": "aws",
                "primary_region": "ap-southeast-1",
                "plan": "payg",
                "budget": 20,
                "tls": true,
                "eviction": true,
            })
        );
        // Basic 認証 (email:api key)
        assert_eq!(
            writes[0].header("authorization"),
            Some("Basic b3BzQGV4YW1wbGUuY29tOnVwc3Rhc2gta2V5")
        );
        let last = sink.0.last().unwrap();
        assert_eq!(
            (last.1, last.2.as_str()),
            (
                Style::Success,
                "Upstash データベースを作成しました: dev-app"
            )
        );
    }

    #[tokio::test]
    async fn test_create_skips_existing_database() {
        // active でなくても、名前が在れば作らない
        let server = server(json!([database("dev-app", "deleted", true)]));
        let http = stub_http(&server);
        let ctx = context();
        let mut sink = Recorded::default();
        Upstash {
            http: &http,
            ctx: &ctx,
        }
        .create(&mut sink)
        .await
        .unwrap();
        assert!(server.writes().is_empty());
        assert_eq!(
            sink.0[0].2,
            "Upstash データベース 'dev-app' は既に存在します"
        );
    }

    #[tokio::test]
    async fn test_create_requires_credentials_and_reports_api_errors() {
        let server = StubServer::start(|_: &Request| Reply::json(401, json!("Unauthorized")));
        let http = stub_http(&server);
        let mut ctx = context();
        let error = Upstash {
            http: &http,
            ctx: &ctx,
        }
        .create(&mut Recorded::default())
        .await
        .unwrap_err();
        assert_eq!(error.to_string(), "Upstash API error: 401 \"Unauthorized\"");

        ctx.api_key = None;
        let error = Upstash {
            http: &http,
            ctx: &ctx,
        }
        .create(&mut Recorded::default())
        .await
        .unwrap_err();
        assert_eq!(
            error.to_string(),
            "UPSTASH_EMAIL と UPSTASH_API_KEY を設定してください"
        );
        assert_eq!(server.requests().len(), 1);
    }

    #[tokio::test]
    async fn test_redis_url() {
        let ctx = context();
        for (databases, want) in [
            (
                json!([database("dev-app", "active", true)]),
                Some("rediss://default:secret@apn1-x.upstash.io:6379"),
            ),
            (
                json!([database("dev-app", "active", false)]),
                Some("redis://default:secret@apn1-x.upstash.io:6379"),
            ),
            // 未準備: database が無い・active でない
            (json!([database("other", "active", true)]), None),
            (json!([database("dev-app", "deleted", true)]), None),
        ] {
            let server = server(databases);
            let http = stub_http(&server);
            let url = Upstash {
                http: &http,
                ctx: &ctx,
            }
            .redis_url()
            .await
            .unwrap();
            assert_eq!(url.as_deref(), want);
            assert!(server.writes().is_empty());
        }
    }

    #[tokio::test]
    async fn test_redis_url_without_credentials_is_not_ready() {
        let server = server(json!([]));
        let http = stub_http(&server);
        let mut ctx = context();
        ctx.email = Some(String::new());
        let url = Upstash {
            http: &http,
            ctx: &ctx,
        }
        .redis_url()
        .await
        .unwrap();
        assert_eq!(url, None);
        assert!(server.requests().is_empty());
    }
}
