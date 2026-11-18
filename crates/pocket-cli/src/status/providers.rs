//! 外部 provider の DB の状態 (Neon / TiDB / Upstash。Python 版 `pocket.provisioning.neon` と
//! `resources/tidb.py` / `resources/upstash.py` の `status`)。
//!
//! 一覧は Python 版と同じく先頭のページだけを見る。資格情報が無いときの扱いは
//! provider ごとに違う (Neon は呼び出し側が skip、TiDB は NOEXIST、Upstash はエラー)

use std::time::Duration;

use anyhow::{anyhow, bail, Context as _, Result};
use magic_pocket_settings::context::{NeonContext, TiDbContext, UpstashContext};
use md5::{Digest, Md5};
use serde_json::Value as Json;

use super::sink::Sink;
use crate::stack::ops::ResourceStatus;

/// provider の API の接続先。既定は本番で、テストだけが localhost の stub に向ける
/// (環境変数などでの差し替えは持たない)
#[derive(Debug, Clone)]
pub struct Endpoints {
    /// 末尾に `/` が付く
    pub neon: String,
    /// 末尾に `/` が付く
    pub tidb_iam: String,
    /// 末尾に `/` が付く
    pub tidb_serverless: String,
    /// 末尾に `/` が付かない
    pub upstash: String,
}

impl Default for Endpoints {
    fn default() -> Self {
        Self {
            neon: "https://console.neon.tech/api/v2/".to_string(),
            tidb_iam: "https://iam.tidbapi.com/v1beta1/".to_string(),
            tidb_serverless: "https://serverless.tidbapi.com/v1beta1/".to_string(),
            upstash: "https://api.upstash.com/v2/redis".to_string(),
        }
    }
}

pub struct Response {
    pub status: u16,
    pub body: String,
}

impl Response {
    pub fn ok(&self) -> bool {
        (200..300).contains(&self.status)
    }

    pub fn json(&self) -> Result<Json> {
        serde_json::from_str(&self.body).context("API の応答が JSON ではありません")
    }
}

/// 要求に付ける認証
#[derive(Debug, Clone, Copy)]
pub enum Auth<'a> {
    /// `Authorization: Bearer` (Neon。`Accept: application/json` も付ける)
    Bearer(&'a str),
    Basic(&'a str, &'a str),
    /// 認証なしで送り、401 の challenge に応えて送り直す (TiDB Cloud)
    Digest(&'a str, &'a str),
}

pub struct Http {
    client: reqwest::Client,
    pub endpoints: Endpoints,
    /// 書き込みの後に provider 側の反映を待つ時間 (Python 版の `time.sleep(2)`)
    pub settle: Duration,
}

impl Http {
    pub fn new() -> Result<Self> {
        Self::with_endpoints(Endpoints::default(), Duration::from_secs(2))
    }

    pub fn with_endpoints(endpoints: Endpoints, settle: Duration) -> Result<Self> {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(30))
            .build()
            .context("HTTP client を作れません")?;
        Ok(Self {
            client,
            endpoints,
            settle,
        })
    }

    /// 認証と JSON の body を付けて送る。4xx / 5xx も error にせず [`Response`] で返す
    pub async fn request(
        &self,
        method: reqwest::Method,
        url: &str,
        auth: Auth<'_>,
        body: Option<&Json>,
    ) -> Result<Response> {
        let build = || {
            let request = self.client.request(method.clone(), url);
            match body {
                Some(body) => request.json(body),
                None => request,
            }
        };
        match auth {
            Auth::Bearer(key) => {
                let request = build()
                    .header(reqwest::header::ACCEPT, "application/json")
                    .bearer_auth(key);
                Ok(self.send(request).await?.0)
            }
            Auth::Basic(user, password) => {
                Ok(self.send(build().basic_auth(user, Some(password))).await?.0)
            }
            Auth::Digest(user, password) => {
                let (first, challenge) = self.send(build()).await?;
                if first.status != 401 {
                    return Ok(first);
                }
                let Some(challenge) = challenge else {
                    return Ok(first);
                };
                let parsed = reqwest::Url::parse(url)?;
                let mut uri = parsed.path().to_string();
                if let Some(query) = parsed.query() {
                    uri.push('?');
                    uri.push_str(query);
                }
                let cnonce = format!("{:016x}", rand_u64());
                let header = digest_authorization(
                    &challenge,
                    user,
                    password,
                    method.as_str(),
                    &uri,
                    &cnonce,
                )?;
                let request = build().header(reqwest::header::AUTHORIZATION, header);
                Ok(self.send(request).await?.0)
            }
        }
    }

    /// 書き込みの後の反映待ち
    pub async fn settle(&self) {
        if !self.settle.is_zero() {
            tokio::time::sleep(self.settle).await;
        }
    }

    async fn send(&self, request: reqwest::RequestBuilder) -> Result<(Response, Option<String>)> {
        let response = request.send().await?;
        let status = response.status().as_u16();
        let challenge = response
            .headers()
            .get(reqwest::header::WWW_AUTHENTICATE)
            .and_then(|value| value.to_str().ok())
            .map(str::to_string);
        let body = response.text().await?;
        Ok((Response { status, body }, challenge))
    }

    async fn get_bearer(&self, url: &str, key: &str) -> Result<Response> {
        self.request(reqwest::Method::GET, url, Auth::Bearer(key), None)
            .await
    }

    async fn get_basic(&self, url: &str, user: &str, password: &str) -> Result<Response> {
        self.request(reqwest::Method::GET, url, Auth::Basic(user, password), None)
            .await
    }

    async fn get_digest(&self, url: &str, user: &str, password: &str) -> Result<Response> {
        self.request(
            reqwest::Method::GET,
            url,
            Auth::Digest(user, password),
            None,
        )
        .await
    }
}

/// cnonce 用の乱数 (予測困難である必要は薄いが、要求ごとに変える)
fn rand_u64() -> u64 {
    use std::collections::hash_map::RandomState;
    use std::hash::{BuildHasher, Hasher};
    RandomState::new().build_hasher().finish()
}

fn md5_hex(text: &str) -> String {
    hex::encode(Md5::digest(text.as_bytes()))
}

/// `WWW-Authenticate: Digest ...` の `key=value` を取り出す (値の引用符は外す)
fn challenge_params(challenge: &str) -> Result<Vec<(String, String)>> {
    let rest = challenge
        .trim()
        .strip_prefix("Digest")
        .or_else(|| challenge.trim().strip_prefix("digest"))
        .ok_or_else(|| anyhow!("Digest 認証の challenge ではありません: {challenge}"))?;
    let mut params = Vec::new();
    let mut chars = rest.chars().peekable();
    loop {
        while chars.peek().is_some_and(|c| c.is_whitespace() || *c == ',') {
            chars.next();
        }
        let mut key = String::new();
        while let Some(c) = chars.next_if(|c| *c != '=') {
            key.push(c);
        }
        if chars.next().is_none() {
            break;
        }
        let mut value = String::new();
        if chars.next_if_eq(&'"').is_some() {
            while let Some(c) = chars.next() {
                match c {
                    '\\' => value.extend(chars.next()),
                    '"' => break,
                    c => value.push(c),
                }
            }
        } else {
            while let Some(c) = chars.next_if(|c| *c != ',') {
                value.push(c);
            }
        }
        params.push((key.trim().to_lowercase(), value.trim().to_string()));
    }
    Ok(params)
}

/// challenge への応答 (`Authorization` header の値)。
///
/// TiDB Cloud が返す `algorithm=MD5` / `qop="auth"` (RFC 7616) に対応する。それ以外の
/// algorithm は黙って誤った応答を作らず、エラーにする
pub fn digest_authorization(
    challenge: &str,
    user: &str,
    password: &str,
    method: &str,
    uri: &str,
    cnonce: &str,
) -> Result<String> {
    let params = challenge_params(challenge)?;
    let get = |name: &str| {
        params
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.as_str())
    };
    let realm = get("realm").unwrap_or_default();
    let nonce =
        get("nonce").ok_or_else(|| anyhow!("Digest 認証の challenge に nonce がありません"))?;
    let algorithm = get("algorithm").unwrap_or("MD5");
    if !algorithm.eq_ignore_ascii_case("MD5") {
        bail!("Digest 認証の algorithm {algorithm} には対応していません (MD5 のみ)");
    }
    let qop_auth = match get("qop") {
        None => false,
        Some(qop) if qop.split(',').any(|q| q.trim() == "auth") => true,
        Some(qop) => bail!("Digest 認証の qop {qop} には対応していません (auth のみ)"),
    };
    let ha1 = md5_hex(&format!("{user}:{realm}:{password}"));
    let ha2 = md5_hex(&format!("{method}:{uri}"));
    let nc = "00000001";
    let response = if qop_auth {
        md5_hex(&format!("{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}"))
    } else {
        md5_hex(&format!("{ha1}:{nonce}:{ha2}"))
    };
    let mut header = format!(
        "Digest username=\"{user}\", realm=\"{realm}\", nonce=\"{nonce}\", uri=\"{uri}\", \
         response=\"{response}\""
    );
    if let Some(opaque) = get("opaque") {
        header.push_str(&format!(", opaque=\"{opaque}\""));
    }
    if get("algorithm").is_some() {
        header.push_str(&format!(", algorithm=\"{algorithm}\""));
    }
    if qop_auth {
        header.push_str(&format!(", qop=\"auth\", nc={nc}, cnonce=\"{cnonce}\""));
    }
    Ok(header)
}

pub fn find_by<'a>(items: &'a Json, key: &str, value: &str) -> Option<&'a Json> {
    items
        .as_array()?
        .iter()
        .find(|item| item[key].as_str() == Some(value))
}

// ---- Neon ----

/// エラー応答から message を取り出す (JSON でない・message が無い応答でも止まらない)
pub fn neon_error_detail(response: &Response) -> String {
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

pub async fn neon_get(
    http: &Http,
    key: &str,
    path: &str,
    sink: &mut dyn Sink,
) -> Result<Option<Json>> {
    let response = http
        .get_bearer(&format!("{}{path}", http.endpoints.neon), key)
        .await?;
    if response.ok() {
        return response.json().map(Some);
    }
    let detail = neon_error_detail(&response);
    if response.status == 404 {
        return Ok(None);
    }
    if response.status == 401 {
        // 診断用に prefix と長さだけ出す
        sink.print(&format!(
            "Used API key: {}...",
            key.chars().take(5).collect::<String>()
        ));
        sink.print(&format!("API key length: {}", key.chars().count()));
    }
    bail!("{}: {detail}", response.status)
}

/// 404 を「無い」と見なさない取得 (一覧系)
pub async fn neon_list(http: &Http, key: &str, path: &str, sink: &mut dyn Sink) -> Result<Json> {
    match neon_get(http, key, path, sink).await? {
        Some(json) => Ok(json),
        None => bail!("404: Neon API {path}"),
    }
}

/// project 名から project id を引く。組織の key は一覧から、project に限った key は
/// 一覧のエラーに入っている project id から解決する
pub async fn neon_project_id(
    http: &Http,
    key: &str,
    neon: &NeonContext,
    sink: &mut dyn Sink,
) -> Result<String> {
    let response = http
        .get_bearer(&format!("{}projects", http.endpoints.neon), key)
        .await?;
    if response.ok() {
        let json = response.json()?;
        return match find_by(&json["projects"], "name", &neon.project_name) {
            Some(project) => Ok(project["id"].as_str().unwrap_or_default().to_string()),
            None => bail!("Neon project '{}' not found", neon.project_name),
        };
    }
    let payload = response.json()?;
    let message = payload["message"].as_str().unwrap_or_default();
    let marker = "subject_project_id:\"";
    let project_id = message
        .find(marker)
        .map(|at| &message[at + marker.len()..])
        .and_then(|rest| rest.split_once('"'))
        .map(|(id, _)| id)
        .filter(|id| !id.is_empty());
    let Some(project_id) = project_id else {
        bail!(
            "Failed to resolve Neon project: {}: {message}",
            response.status
        );
    };
    let json = neon_list(http, key, &format!("projects/{project_id}"), sink).await?;
    let actual = json["project"]["name"].as_str().unwrap_or_default();
    if actual != neon.project_name {
        bail!(
            "Neon project name mismatch: config='{}', actual='{actual}'",
            neon.project_name
        );
    }
    Ok(json["project"]["id"]
        .as_str()
        .unwrap_or_default()
        .to_string())
}

/// branch_name が未指定なのに stage 名の branch があるときの警告
pub fn neon_stage_branch_warning(neon: &NeonContext, branches: &Json) -> Option<String> {
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

/// branch・database・endpoint・role がそろっていれば COMPLETED、欠けていれば NOEXIST
pub async fn neon_status(
    http: &Http,
    neon: &NeonContext,
    sink: &mut dyn Sink,
) -> Result<ResourceStatus> {
    let key = neon.api_key.as_deref().unwrap_or_default();
    let project_id = neon_project_id(http, key, neon, sink).await?;
    let base = format!("projects/{project_id}");
    let branches = neon_list(http, key, &format!("{base}/branches"), sink).await?;
    if let Some(warning) = neon_stage_branch_warning(neon, &branches["branches"]) {
        sink.warning(&warning);
    }
    let Some(branch) = find_by(&branches["branches"], "name", &neon.branch_name) else {
        return Ok(ResourceStatus::NoExist);
    };
    let branch_id = branch["id"].as_str().unwrap_or_default();
    let branch_base = format!("{base}/branches/{branch_id}");
    let databases = neon_list(http, key, &format!("{branch_base}/databases"), sink).await?;
    let database = find_by(&databases["databases"], "name", &neon.name).is_some();
    let endpoints = neon_list(http, key, &format!("{base}/endpoints"), sink).await?;
    let endpoint = endpoints["endpoints"].as_array().is_some_and(|list| {
        list.iter().any(|endpoint| {
            endpoint["branch_id"].as_str() == Some(branch_id)
                && endpoint["type"].as_str() == Some("read_write")
        })
    });
    let role_path = format!("{branch_base}/roles/{}", neon.role_name);
    let role = neon_get(http, key, &role_path, sink).await?.is_some();
    Ok(if database && endpoint && role {
        ResourceStatus::Completed
    } else {
        ResourceStatus::NoExist
    })
}

// ---- TiDB ----

pub async fn tidb_get(http: &Http, tidb: &TiDbContext, url: &str) -> Result<Json> {
    let user = tidb.public_key.as_deref().unwrap_or_default();
    let password = tidb.private_key.as_deref().unwrap_or_default();
    let response = http.get_digest(url, user, password).await?;
    if !response.ok() {
        bail!("{}: {}", response.status, response.body);
    }
    response.json()
}

/// 使う project を選ぶ。project の指定が無ければ、1 つだけのときにそれを使う
pub fn tidb_select_project<'a>(
    tidb: &TiDbContext,
    projects: &'a [Json],
) -> Result<Option<&'a Json>> {
    if let Some(name) = tidb.tidb_project.as_deref().filter(|n| !n.is_empty()) {
        return Ok(projects.iter().find(|p| p["name"].as_str() == Some(name)));
    }
    match projects {
        [] => Ok(None),
        [only] => Ok(Some(only)),
        _ => {
            let names: Vec<&str> = projects
                .iter()
                .map(|p| p["name"].as_str().unwrap_or_default())
                .collect();
            let listed = names
                .iter()
                .map(|n| format!("'{n}'"))
                .collect::<Vec<_>>()
                .join(", ");
            bail!(
                "複数の TiDB Cloud プロジェクトが見つかりました: [{listed}]\n\
                 pocket.toml の [tidb] で project を指定してください。\n\
                 例: project = \"{}\"",
                names[0]
            )
        }
    }
}

/// cluster の state から status を決める。cluster が無ければ NOEXIST
pub fn tidb_cluster_status(tidb: &TiDbContext, clusters: &[Json]) -> ResourceStatus {
    let cluster = clusters
        .iter()
        .find(|c| c["displayName"].as_str() == Some(tidb.cluster_name.as_str()));
    match cluster {
        None => ResourceStatus::NoExist,
        Some(cluster) if cluster["state"].as_str() == Some("ACTIVE") => ResourceStatus::Completed,
        Some(_) => ResourceStatus::Progress,
    }
}

pub fn json_list(value: &Json) -> &[Json] {
    value.as_array().map(Vec::as_slice).unwrap_or_default()
}

/// project の id (数値でも文字列でも同じ文字列にする。Python の `str(p["id"])`)
pub fn id_text(value: &Json) -> String {
    match value.as_str() {
        Some(text) => text.to_string(),
        None => value.to_string(),
    }
}

pub async fn tidb_status(http: &Http, tidb: &TiDbContext) -> Result<ResourceStatus> {
    let has = |value: &Option<String>| value.as_deref().is_some_and(|v| !v.is_empty());
    if !has(&tidb.public_key) || !has(&tidb.private_key) {
        return Ok(ResourceStatus::NoExist);
    }
    let projects = tidb_get(http, tidb, &format!("{}projects", http.endpoints.tidb_iam)).await?;
    let Some(project) = tidb_select_project(tidb, json_list(&projects["projects"]))? else {
        return Ok(ResourceStatus::NoExist);
    };
    let url = format!(
        "{}clusters?filter=projectId={}",
        http.endpoints.tidb_serverless,
        id_text(&project["id"])
    );
    let clusters = tidb_get(http, tidb, &url).await?;
    Ok(tidb_cluster_status(tidb, json_list(&clusters["clusters"])))
}

// ---- Upstash ----

/// database が active なら COMPLETED、無い・active でないなら NOEXIST
pub fn upstash_database_status(upstash: &UpstashContext, databases: &[Json]) -> ResourceStatus {
    let database = databases
        .iter()
        .find(|db| db["database_name"].as_str() == Some(upstash.database_name.as_str()));
    match database {
        Some(db) if db["state"].as_str() == Some("active") => ResourceStatus::Completed,
        _ => ResourceStatus::NoExist,
    }
}

pub async fn upstash_status(http: &Http, upstash: &UpstashContext) -> Result<ResourceStatus> {
    let email = upstash.email.as_deref().filter(|v| !v.is_empty());
    let api_key = upstash.api_key.as_deref().filter(|v| !v.is_empty());
    let (Some(email), Some(api_key)) = (email, api_key) else {
        bail!("UPSTASH_EMAIL と UPSTASH_API_KEY を設定してください");
    };
    let url = format!("{}/databases", http.endpoints.upstash);
    let response = http.get_basic(&url, email, api_key).await?;
    if !response.ok() {
        bail!("Upstash API error: {} {}", response.status, response.body);
    }
    Ok(upstash_database_status(
        upstash,
        json_list(&response.json()?),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use magic_pocket_settings::model::ProvisioningMode;
    use serde_json::json;

    #[test]
    fn test_digest_authorization_rfc2617_example() {
        // RFC 2617 3.5 の例 (MD5 / qop=auth)
        let challenge = "Digest realm=\"testrealm@host.com\", qop=\"auth,auth-int\", \
                         nonce=\"dcd98b7102dd2f0e8b11d0f600bfb0c093\", \
                         opaque=\"5ccc069c403ebaf9f0171e9517f40e41\"";
        let header = digest_authorization(
            challenge,
            "Mufasa",
            "Circle Of Life",
            "GET",
            "/dir/index.html",
            "0a4f113b",
        )
        .unwrap();
        assert!(
            header.contains("response=\"6629fae49393a05397450978507c4ef1\""),
            "{header}"
        );
        assert!(header.contains("qop=\"auth\", nc=00000001, cnonce=\"0a4f113b\""));
        assert!(header.contains("opaque=\"5ccc069c403ebaf9f0171e9517f40e41\""));
    }

    #[test]
    fn test_digest_authorization_tidb_challenge_shape() {
        // TiDB Cloud が返す形 (引用符なしの algorithm、空の domain、stale)
        let challenge = "Digest realm=\"tidb.cloud\", domain=\"\", \
                         nonce=\"e1b9494e87347ade5366edcb24423951\", algorithm=MD5, \
                         qop=\"auth\", stale=false";
        let header =
            digest_authorization(challenge, "pub", "priv", "GET", "/v1beta1/projects", "abc")
                .unwrap();
        let ha1 = md5_hex("pub:tidb.cloud:priv");
        let ha2 = md5_hex("GET:/v1beta1/projects");
        let want = md5_hex(&format!(
            "{ha1}:e1b9494e87347ade5366edcb24423951:00000001:abc:auth:{ha2}"
        ));
        assert!(header.contains(&format!("response=\"{want}\"")), "{header}");
        assert!(header.contains("algorithm=\"MD5\""));
    }

    #[test]
    fn test_digest_authorization_rejects_unsupported() {
        let sha = "Digest realm=\"r\", nonce=\"n\", algorithm=SHA-256, qop=\"auth\"";
        let error = digest_authorization(sha, "u", "p", "GET", "/", "c").unwrap_err();
        assert!(error.to_string().contains("SHA-256"), "{error}");
        let auth_int = "Digest realm=\"r\", nonce=\"n\", qop=\"auth-int\"";
        assert!(digest_authorization(auth_int, "u", "p", "GET", "/", "c").is_err());
        assert!(digest_authorization("Basic realm=\"r\"", "u", "p", "GET", "/", "c").is_err());
    }

    fn tidb(project: Option<&str>) -> TiDbContext {
        TiDbContext {
            public_key: Some("pub".to_string()),
            private_key: Some("priv".to_string()),
            tidb_project: project.map(str::to_string),
            cluster_name: "dev-app".to_string(),
            database_name: "app".to_string(),
            region: "ap-northeast-1".to_string(),
            project_name: "app".to_string(),
            provisioning: ProvisioningMode::Deploy,
        }
    }

    #[test]
    fn test_tidb_select_project() {
        let one = [json!({"id": 1, "name": "a"})];
        let two = [
            json!({"id": 1, "name": "a"}),
            json!({"id": "2", "name": "b"}),
        ];
        assert_eq!(tidb_select_project(&tidb(None), &[]).unwrap(), None);
        assert_eq!(
            tidb_select_project(&tidb(None), &one).unwrap(),
            Some(&one[0])
        );
        assert_eq!(
            tidb_select_project(&tidb(Some("b")), &two).unwrap(),
            Some(&two[1])
        );
        assert_eq!(tidb_select_project(&tidb(Some("c")), &two).unwrap(), None);
        let error = tidb_select_project(&tidb(None), &two).unwrap_err();
        assert_eq!(
            error.to_string(),
            "複数の TiDB Cloud プロジェクトが見つかりました: ['a', 'b']\n\
             pocket.toml の [tidb] で project を指定してください。\n\
             例: project = \"a\""
        );
        assert_eq!(id_text(&one[0]["id"]), "1");
        assert_eq!(id_text(&two[1]["id"]), "2");
    }

    #[test]
    fn test_tidb_cluster_status() {
        // Python 版 tests/test_status_classification.py: ACTIVE 以外は PROGRESS
        let ctx = tidb(None);
        assert_eq!(tidb_cluster_status(&ctx, &[]), ResourceStatus::NoExist);
        for (state, want) in [
            ("ACTIVE", ResourceStatus::Completed),
            ("CREATING", ResourceStatus::Progress),
            ("DELETING", ResourceStatus::Progress),
        ] {
            let clusters = [
                json!({"displayName": "other", "state": "ACTIVE"}),
                json!({"displayName": "dev-app", "state": state}),
            ];
            assert_eq!(tidb_cluster_status(&ctx, &clusters), want, "{state}");
        }
        let no_state = [json!({"displayName": "dev-app"})];
        assert_eq!(
            tidb_cluster_status(&ctx, &no_state),
            ResourceStatus::Progress
        );
    }

    #[test]
    fn test_upstash_database_status() {
        let ctx = UpstashContext {
            email: Some("e".to_string()),
            api_key: Some("k".to_string()),
            database_name: "dev-app".to_string(),
            budget: 20,
            provisioning: ProvisioningMode::Deploy,
        };
        let active = [json!({"database_name": "dev-app", "state": "active"})];
        let deleted = [json!({"database_name": "dev-app", "state": "deleted"})];
        assert_eq!(
            upstash_database_status(&ctx, &active),
            ResourceStatus::Completed
        );
        assert_eq!(
            upstash_database_status(&ctx, &deleted),
            ResourceStatus::NoExist
        );
        assert_eq!(upstash_database_status(&ctx, &[]), ResourceStatus::NoExist);
    }

    fn neon(specified: bool, branch: &str) -> NeonContext {
        NeonContext {
            pg_version: 17,
            api_key: Some("key".to_string()),
            project_name: "app".to_string(),
            branch_name: branch.to_string(),
            branch_name_specified: specified,
            stage: Some("dev".to_string()),
            parent_branch_name: None,
            name: "app".to_string(),
            role_name: "app".to_string(),
            provisioning: ProvisioningMode::Deploy,
        }
    }

    #[test]
    fn test_neon_stage_branch_warning() {
        let branches = json!([{"name": "main"}, {"name": "dev"}]);
        let warning = neon_stage_branch_warning(&neon(false, "main"), &branches).unwrap();
        assert!(
            warning.starts_with("[neon] branch_name が未指定のため default branch 'main' を使いますが、project 'app' には stage 名の branch 'dev' も存在します。"),
            "{warning}"
        );
        assert!(warning.ends_with("[dev.neon] に branch_name = \"dev\" を明示してください。"));
        // 明示している・stage 名の branch を使っている・stage 名の branch が無い → 警告なし
        assert_eq!(
            neon_stage_branch_warning(&neon(true, "main"), &branches),
            None
        );
        assert_eq!(
            neon_stage_branch_warning(&neon(false, "dev"), &branches),
            None
        );
        let only_main = json!([{"name": "main"}]);
        assert_eq!(
            neon_stage_branch_warning(&neon(false, "main"), &only_main),
            None
        );
    }

    #[test]
    fn test_neon_error_detail() {
        let response = |body: &str| Response {
            status: 500,
            body: body.to_string(),
        };
        assert_eq!(
            neon_error_detail(&response("<html>")),
            "<non-JSON response>"
        );
        assert_eq!(
            neon_error_detail(&response(r#"{"message": "boom"}"#)),
            "boom"
        );
        assert_eq!(
            neon_error_detail(&response(r#"{"code": 1}"#)),
            r#"{"code": 1}"#
        );
    }
}
