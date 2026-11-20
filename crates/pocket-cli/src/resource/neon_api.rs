//! Neon の API (Python 版 `pocket.provisioning.neon` の `NeonApi` / `Neon`)。
//!
//! 要求の method・path・body と、応答による分岐は Python 版と同じ。Python 版は
//! `cached_property` で branch / role などを 1 回だけ引き、作成の後に捨てて引き直す。
//! ここでは `Option<Option<T>>` の cache で同じ順に引く。
//!
//! API key はログ・エラーに出さない (401 のときだけ、Python 版と同じく先頭 5 文字と
//! 長さを stdout に出す)

use std::time::Duration;

use anyhow::{bail, Context as _, Result};
use magic_pocket_settings::context::NeonContext;
use reqwest::Method;
use serde_json::{json, Value as Json};

use crate::status::sink::Sink;

pub const NEON_ENDPOINT: &str = "https://console.neon.tech/api/v2/";

/// GET が 404 を返した (Python の `NeonNotFound`)
#[derive(Debug)]
pub struct NeonNotFound(pub String);

impl std::fmt::Display for NeonNotFound {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for NeonNotFound {}

pub struct Response {
    pub status: u16,
    pub body: String,
}

impl Response {
    fn ok(&self) -> bool {
        (200..300).contains(&self.status)
    }

    /// 空の body は `{}` (Python の `_HttpResponse.json`)
    fn json(&self) -> Result<Json> {
        if self.body.is_empty() {
            return Ok(json!({}));
        }
        serde_json::from_str(&self.body).context("Neon API の応答が JSON ではありません")
    }
}

/// エラー応答から message を取り出す (JSON でない・message が無い応答でも止まらない)
fn error_detail(response: &Response) -> String {
    match serde_json::from_str::<Json>(&response.body) {
        Err(_) => "<non-JSON response>".to_string(),
        Ok(payload) => match payload.get("message") {
            Some(message) if payload.is_object() => match message.as_str() {
                Some(text) => text.to_string(),
                None => message.to_string(),
            },
            _ => magic_pocket_settings::pyjson::dumps(&payload)
                .chars()
                .take(200)
                .collect(),
        },
    }
}

pub struct NeonApi {
    client: reqwest::Client,
    key: String,
    /// 末尾に `/` が付く
    pub endpoint: String,
    /// 書き込みの後に Neon 側の反映を待つ時間 (Python 版の `time.sleep(2)`)
    pub settle: Duration,
}

impl NeonApi {
    pub fn new(key: &str) -> Result<Self> {
        Self::with_endpoint(key, NEON_ENDPOINT, Duration::from_secs(2))
    }

    pub fn with_endpoint(key: &str, endpoint: &str, settle: Duration) -> Result<Self> {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(30))
            .build()
            .context("HTTP client を作れません")?;
        Ok(Self {
            client,
            key: key.to_string(),
            endpoint: endpoint.to_string(),
            settle,
        })
    }

    /// 認証を付けて送る。4xx / 5xx も error にせず返す (Python の `_http_request`)
    pub async fn raw(&self, method: Method, path: &str, data: Option<&Json>) -> Result<Response> {
        let url = format!("{}{path}", self.endpoint);
        let mut request = self
            .client
            .request(method, &url)
            .header(reqwest::header::ACCEPT, "application/json")
            .bearer_auth(&self.key);
        if let Some(data) = data {
            request = request.json(data);
        }
        // reqwest の error は URL を含むが、key は header なので含まない
        let response = request.send().await.context("Neon API に接続できません")?;
        let status = response.status().as_u16();
        let body = response
            .text()
            .await
            .context("Neon API の応答を読めません")?;
        Ok(Response { status, body })
    }

    /// Python の `NeonApi._request`。2xx 以外は error (GET の 404 は [`NeonNotFound`])
    pub async fn request(
        &self,
        method: Method,
        path: &str,
        data: Option<&Json>,
        sink: &mut dyn Sink,
    ) -> Result<Json> {
        let is_get = method == Method::GET;
        let response = self.raw(method, path, data).await?;
        if response.ok() {
            if !is_get && !self.settle.is_zero() {
                // Neon 側の反映待ち (operations の polling の簡易な代わり)
                tokio::time::sleep(self.settle).await;
            }
            return response.json();
        }
        let detail = error_detail(&response);
        if is_get && response.status == 404 {
            return Err(NeonNotFound(format!("{}: {detail}", response.status)).into());
        }
        if response.status == 401 {
            self.print_auth_hint(sink);
        }
        bail!("{}: {detail}", response.status)
    }

    fn print_auth_hint(&self, sink: &mut dyn Sink) {
        if self.key.is_empty() {
            sink.print("NEON_API_KEY が未設定です (Authorization: Bearer None)。");
            return;
        }
        // 診断用に先頭と長さだけ出す (末尾まで出すと secret の一部が漏れる)
        let prefix: String = self.key.chars().take(5).collect();
        sink.print(&format!("Used API key: {prefix}..."));
        sink.print(&format!("API key length: {}", self.key.chars().count()));
    }

    pub async fn get(&self, path: &str, sink: &mut dyn Sink) -> Result<Json> {
        self.request(Method::GET, path, None, sink).await
    }

    pub async fn post(&self, path: &str, data: &Json, sink: &mut dyn Sink) -> Result<Json> {
        self.request(Method::POST, path, Some(data), sink).await
    }

    pub async fn delete(&self, path: &str, sink: &mut dyn Sink) -> Result<Json> {
        self.request(Method::DELETE, path, None, sink).await
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Branch {
    pub id: String,
    pub name: String,
    /// root branch (project を作ったときの default) には無い
    pub parent_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Endpoint {
    pub id: String,
    pub host: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Role {
    pub name: String,
    pub password: Option<String>,
}

fn text(value: &Json, key: &str) -> String {
    value[key].as_str().unwrap_or_default().to_string()
}

fn branch_of(value: &Json) -> Branch {
    Branch {
        id: text(value, "id"),
        name: text(value, "name"),
        parent_id: value["parent_id"].as_str().map(str::to_string),
    }
}

fn find_by<'a>(items: &'a Json, key: &str, value: &str) -> Option<&'a Json> {
    items
        .as_array()?
        .iter()
        .find(|item| item[key].as_str() == Some(value))
}

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

/// `stage_branch` の警告 (branch_name 未指定なのに stage 名の branch がある)
fn stage_branch_warning(neon: &NeonContext, branches: &Json) -> Option<String> {
    let stage = neon.stage.as_deref().filter(|s| !s.is_empty())?;
    if neon.branch_name_specified || stage == neon.branch_name {
        return None;
    }
    find_by(branches, "name", stage)?;
    Some(format!(
        "[neon] branch_name が未指定のため default branch '{}' を使いますが、\
         project '{}' には stage 名の branch '{stage}' も存在します。\
         0.5.0 で branch_name 省略時のデフォルトが stage 名から default branch \
         に変わりました。接続先が branch '{stage}' のつもりなら、pocket.toml の \
         [{stage}.neon] に branch_name = \"{stage}\" を明示してください。",
        neon.branch_name, neon.project_name
    ))
}

/// project 内の branch / role / database / endpoint (Python の `Neon`)
pub struct Neon<'a> {
    pub api: &'a NeonApi,
    pub ctx: &'a NeonContext,
    project_id: Option<String>,
    branch: Option<Option<Branch>>,
    role: Option<Option<Role>>,
    database: Option<bool>,
    endpoint: Option<Option<Endpoint>>,
    warned: bool,
}

impl<'a> Neon<'a> {
    pub fn new(api: &'a NeonApi, ctx: &'a NeonContext) -> Self {
        Self {
            api,
            ctx,
            project_id: None,
            branch: None,
            role: None,
            database: None,
            endpoint: None,
            warned: false,
        }
    }

    /// project 名から project の id を引く。組織の key は一覧から、project に限った key は
    /// 一覧のエラーに入っている project の id から解決する
    pub async fn project_id(&mut self, sink: &mut dyn Sink) -> Result<String> {
        if let Some(id) = self.project_id.as_ref() {
            return Ok(id.clone());
        }
        let name = &self.ctx.project_name;
        let response = self.api.raw(Method::GET, "projects", None).await?;
        let id = if response.ok() {
            let json = response.json()?;
            match find_by(&json["projects"], "name", name) {
                Some(project) => text(project, "id"),
                None => bail!("Neon project '{name}' not found"),
            }
        } else {
            let payload = response.json().unwrap_or(Json::Null);
            let message = payload["message"].as_str().unwrap_or_default();
            let marker = "subject_project_id:\"";
            let found = message
                .find(marker)
                .map(|at| &message[at + marker.len()..])
                .and_then(|rest| rest.split_once('"'))
                .map(|(id, _)| id.to_string())
                .filter(|id| !id.is_empty());
            let Some(project_id) = found else {
                bail!(
                    "Failed to resolve Neon project: {}: {message}",
                    response.status
                );
            };
            let json = self
                .api
                .get(&format!("projects/{project_id}"), sink)
                .await?;
            let actual = text(&json["project"], "name");
            if actual != *name {
                bail!("Neon project name mismatch: config='{name}', actual='{actual}'");
            }
            text(&json["project"], "id")
        };
        self.project_id = Some(id.clone());
        Ok(id)
    }

    /// project の全 branch
    pub async fn branches(&mut self, sink: &mut dyn Sink) -> Result<Json> {
        let project_id = self.project_id(sink).await?;
        let mut json = self
            .api
            .get(&format!("projects/{project_id}/branches"), sink)
            .await?;
        Ok(json["branches"].take())
    }

    pub async fn branch(&mut self, sink: &mut dyn Sink) -> Result<Option<Branch>> {
        if let Some(branch) = self.branch.as_ref() {
            return Ok(branch.clone());
        }
        let branches = self.branches(sink).await?;
        if !self.warned {
            if let Some(warning) = stage_branch_warning(self.ctx, &branches) {
                self.warned = true;
                sink.warning(&warning);
            }
        }
        let branch = find_by(&branches, "name", &self.ctx.branch_name).map(branch_of);
        self.branch = Some(branch.clone());
        Ok(branch)
    }

    /// branch を作るときの親。指定が無ければ `None` (Neon の default branch から分かれる)。
    /// 指定があるのに見つからなければ止める (黙って default から分けると事故になる)
    async fn parent_branch(&mut self, sink: &mut dyn Sink) -> Result<Option<Branch>> {
        let Some(parent) = self
            .ctx
            .parent_branch_name
            .clone()
            .filter(|n| !n.is_empty())
        else {
            return Ok(None);
        };
        let branches = self.branches(sink).await?;
        match find_by(&branches, "name", &parent) {
            Some(branch) => Ok(Some(branch_of(branch))),
            None => bail!(
                "Neon parent branch '{parent}' not found in project '{}'",
                self.ctx.project_name
            ),
        }
    }

    /// `projects/<id>/branches/<id>` (branch が無ければ止める)
    async fn branch_path(&mut self, sink: &mut dyn Sink) -> Result<String> {
        let project_id = self.project_id(sink).await?;
        let Some(branch) = self.branch(sink).await? else {
            bail!("branch not found");
        };
        Ok(format!("projects/{project_id}/branches/{}", branch.id))
    }

    pub async fn role(&mut self, sink: &mut dyn Sink) -> Result<Option<Role>> {
        if let Some(role) = self.role.as_ref() {
            return Ok(role.clone());
        }
        let role = if self.branch(sink).await?.is_none() {
            None
        } else {
            let path = format!(
                "{}/roles/{}",
                self.branch_path(sink).await?,
                self.ctx.role_name
            );
            match self.api.get(&path, sink).await {
                Ok(json) => Some(Role {
                    name: text(&json["role"], "name"),
                    password: json["role"]["password"].as_str().map(str::to_string),
                }),
                Err(error) if error.is::<NeonNotFound>() => None,
                Err(error) => return Err(error),
            }
        };
        self.role = Some(role.clone());
        Ok(role)
    }

    pub async fn database(&mut self, sink: &mut dyn Sink) -> Result<bool> {
        if let Some(found) = self.database {
            return Ok(found);
        }
        let found = if self.branch(sink).await?.is_none() {
            false
        } else {
            let path = format!("{}/databases", self.branch_path(sink).await?);
            let json = self.api.get(&path, sink).await?;
            find_by(&json["databases"], "name", &self.ctx.name).is_some()
        };
        self.database = Some(found);
        Ok(found)
    }

    /// 書き込み用の endpoint (read replica の read_only は選ばない)
    pub async fn endpoint(&mut self, sink: &mut dyn Sink) -> Result<Option<Endpoint>> {
        if let Some(endpoint) = self.endpoint.as_ref() {
            return Ok(endpoint.clone());
        }
        let endpoint = match self.branch(sink).await? {
            None => None,
            Some(branch) => {
                let project_id = self.project_id(sink).await?;
                let json = self
                    .api
                    .get(&format!("projects/{project_id}/endpoints"), sink)
                    .await?;
                json["endpoints"].as_array().and_then(|list| {
                    list.iter()
                        .find(|e| {
                            e["branch_id"].as_str() == Some(branch.id.as_str())
                                && e["type"].as_str() == Some("read_write")
                        })
                        .map(|e| Endpoint {
                            id: text(e, "id"),
                            host: text(e, "host"),
                        })
                })
            }
        };
        self.endpoint = Some(endpoint.clone());
        Ok(endpoint)
    }

    /// branch・database・endpoint・role がそろっているか (Python の `working`)
    pub async fn working(&mut self, sink: &mut dyn Sink) -> Result<bool> {
        let branch = self.branch(sink).await?.is_some();
        let database = self.database(sink).await?;
        let endpoint = self.endpoint(sink).await?.is_some();
        let role = self.role(sink).await?.is_some();
        Ok(branch && database && endpoint && role)
    }

    /// branch (無ければ作る) → role → database の順に確保する
    pub async fn create(&mut self, sink: &mut dyn Sink) -> Result<()> {
        if self.branch(sink).await?.is_none() {
            let parent = self.parent_branch(sink).await?;
            self.create_branch(parent.as_ref(), sink).await?;
        }
        self.ensure_role(sink).await?;
        self.ensure_database(sink).await
    }

    /// branch を作る (既にあれば何もしない)。`base` があればそこから分かれる
    pub async fn create_branch(
        &mut self,
        base: Option<&Branch>,
        sink: &mut dyn Sink,
    ) -> Result<()> {
        if self.branch(sink).await?.is_some() {
            return Ok(());
        }
        // 作った後に引き直す
        self.branch = None;
        self.endpoint = None;
        let mut branch = json!({"name": self.ctx.branch_name});
        if let Some(base) = base {
            branch["parent_id"] = json!(base.id);
        }
        let body = json!({"branch": branch, "endpoints": [{"type": "read_write"}]});
        let project_id = self.project_id(sink).await?;
        self.api
            .post(&format!("projects/{project_id}/branches"), &body, sink)
            .await?;
        Ok(())
    }

    pub async fn ensure_role(&mut self, sink: &mut dyn Sink) -> Result<()> {
        if self.role(sink).await?.is_some() {
            return Ok(());
        }
        self.role = None;
        let path = format!("{}/roles", self.branch_path(sink).await?);
        let body = json!({"role": {"name": self.ctx.role_name}});
        self.api.post(&path, &body, sink).await?;
        self.set_role_password(sink).await
    }

    /// role の password を (応答に無ければ reveal の API で) 取って cache に置く
    async fn set_role_password(&mut self, sink: &mut dyn Sink) -> Result<()> {
        let Some(mut role) = self.role(sink).await? else {
            bail!("Create role first");
        };
        if role.password.is_none() {
            let path = format!(
                "{}/roles/{}/reveal_password",
                self.branch_path(sink).await?,
                role.name
            );
            let json = self.api.get(&path, sink).await?;
            role.password = Some(text(&json, "password"));
            self.role = Some(Some(role));
        }
        Ok(())
    }

    pub async fn ensure_database(&mut self, sink: &mut dyn Sink) -> Result<()> {
        if !self.database(sink).await? {
            self.create_database(sink).await?;
        }
        Ok(())
    }

    async fn create_database(&mut self, sink: &mut dyn Sink) -> Result<()> {
        if !self.database(sink).await? {
            self.database = None;
        }
        let path = format!("{}/databases", self.branch_path(sink).await?);
        let body = json!({"database": {"name": self.ctx.name, "owner_name": self.ctx.role_name}});
        self.api.post(&path, &body, sink).await?;
        Ok(())
    }

    /// database を消して作り直す
    pub async fn reset_database(&mut self, sink: &mut dyn Sink) -> Result<()> {
        if self.database(sink).await? {
            let path = format!(
                "{}/databases/{}",
                self.branch_path(sink).await?,
                self.ctx.name
            );
            self.api.delete(&path, sink).await?;
        }
        self.create_database(sink).await
    }

    fn build_url(&self, password: &str, endpoint: &Endpoint) -> String {
        format!(
            "postgres://{}:{password}@{}:5432/{}?sslmode=require",
            quote(&self.ctx.role_name),
            endpoint.host,
            self.ctx.name
        )
    }

    /// 接続 URL (password 込み)。読む側 (pocket.django の db_url) は unquote するので
    /// 必ず quote する
    pub async fn database_url(&mut self, sink: &mut dyn Sink) -> Result<String> {
        let role = self.role(sink).await?;
        let endpoint = self.endpoint(sink).await?;
        let (Some(role), Some(endpoint)) = (role, endpoint) else {
            bail!("Create role and endpoint first");
        };
        if role.password.is_none() {
            self.set_role_password(sink).await?;
        }
        let password = self
            .role(sink)
            .await?
            .and_then(|role| role.password)
            .unwrap_or_default();
        Ok(self.build_url(&quote(&password), &endpoint))
    }

    /// password を伏せた表示用の URL (reveal の API を呼ばない)
    pub async fn masked_database_url(&mut self, sink: &mut dyn Sink) -> Result<String> {
        let role = self.role(sink).await?;
        let endpoint = self.endpoint(sink).await?;
        let (Some(_), Some(endpoint)) = (role, endpoint) else {
            bail!("Create role and endpoint first");
        };
        Ok(self.build_url("****", &endpoint))
    }

    pub async fn delete_branch(&mut self, sink: &mut dyn Sink) -> Result<()> {
        let endpoint = self.endpoint(sink).await?;
        let branch = self.branch(sink).await?;
        let (Some(endpoint), Some(branch)) = (endpoint, branch) else {
            bail!("Branch or endpoint not found. Something is wrong.");
        };
        let project_id = self.project_id(sink).await?;
        self.api
            .delete(
                &format!("projects/{project_id}/endpoints/{}", endpoint.id),
                sink,
            )
            .await?;
        self.api
            .delete(
                &format!("projects/{project_id}/branches/{}", branch.id),
                sink,
            )
            .await?;
        Ok(())
    }

    /// project を丸ごと消す (root branch を消す唯一の手段)
    pub async fn delete_project(&mut self, sink: &mut dyn Sink) -> Result<()> {
        let project_id = self.project_id(sink).await?;
        self.api
            .delete(&format!("projects/{project_id}"), sink)
            .await?;
        Ok(())
    }

    /// branch の消し方。root branch は branch 単位で消せない (422) ので、他の branch が
    /// 無ければ project ごと、あれば消さない
    pub async fn destroy_plan(&mut self, sink: &mut dyn Sink) -> Result<DestroyPlan> {
        let Some(branch) = self.branch(sink).await? else {
            return Err(
                NeonNotFound(format!("branch '{}' not found", self.ctx.branch_name)).into(),
            );
        };
        if branch.parent_id.is_some() {
            return Ok(DestroyPlan::Branch);
        }
        let branches = self.branches(sink).await?;
        let others = branches
            .as_array()
            .into_iter()
            .flatten()
            .any(|b| b["id"].as_str() != Some(branch.id.as_str()));
        Ok(if others {
            DestroyPlan::Blocked
        } else {
            DestroyPlan::Project
        })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DestroyPlan {
    Branch,
    Project,
    Blocked,
}

/// store-url が保存する URL と、表示用の解決結果 (Python の `EnsureUrlInfo`)
pub struct EnsureUrlInfo {
    pub url: String,
    pub endpoint_host: String,
    /// この実行で branch を作った (空の branch に接続する)
    pub branch_created: bool,
}

/// branch / role / database を確保して接続 URL を出す。確保の後の状態を確実に反映する
/// ため、新しい [`Neon`] で URL を出す (Python の `ensure_url_for_context_with_info`)
pub async fn ensure_url(
    api: &NeonApi,
    ctx: &NeonContext,
    sink: &mut dyn Sink,
) -> Result<EnsureUrlInfo> {
    let mut neon = Neon::new(api, ctx);
    let branch_created = neon.branch(sink).await?.is_none();
    neon.create(sink).await?;
    let mut fresh = Neon::new(api, ctx);
    let url = fresh.database_url(sink).await?;
    let endpoint_host = fresh
        .endpoint(sink)
        .await?
        .map(|endpoint| endpoint.host)
        .unwrap_or_default();
    Ok(EnsureUrlInfo {
        url,
        endpoint_host,
        branch_created,
    })
}
