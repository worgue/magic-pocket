//! deploy の手順のテストが共有する fake (secret store)。

use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};

use anyhow::Result;
use magic_pocket_settings::context::SecretsContext;
use magic_pocket_settings::model::StoreType;
use serde_json::Value as Json;

use super::secret_store::{ManagedSecrets, SecretStore};

/// pocket_key ごとの managed secret と、stored user secret の名前を持つ fake
#[derive(Default)]
pub struct FakeStore {
    pub managed: RefCell<BTreeMap<String, ManagedSecrets>>,
    pub user: BTreeSet<String>,
    pub calls: RefCell<Vec<String>>,
}

impl FakeStore {
    pub fn with(self, pocket_key: &str, secrets: Json) -> Self {
        let Json::Object(map) = secrets else {
            panic!("object が必要です");
        };
        self.managed
            .borrow_mut()
            .insert(pocket_key.to_string(), map);
        self
    }

    pub fn secrets(&self, pocket_key: &str) -> ManagedSecrets {
        self.managed
            .borrow()
            .get(pocket_key)
            .cloned()
            .unwrap_or_default()
    }

    pub fn keys(&self, pocket_key: &str) -> Vec<String> {
        self.secrets(pocket_key).keys().cloned().collect()
    }
}

impl SecretStore for FakeStore {
    async fn read(&self, view: &SecretsContext) -> Result<ManagedSecrets> {
        Ok(self.secrets(&view.pocket_key))
    }

    async fn update(&self, view: &SecretsContext, secrets: &ManagedSecrets) -> Result<()> {
        let keys: Vec<&str> = secrets.keys().map(String::as_str).collect();
        self.calls
            .borrow_mut()
            .push(format!("update {} {}", view.pocket_key, keys.join(",")));
        self.managed
            .borrow_mut()
            .insert(view.pocket_key.clone(), secrets.clone());
        Ok(())
    }

    async fn delete_keys(&self, view: &SecretsContext, keys: &BTreeSet<String>) -> Result<()> {
        let names: Vec<&str> = keys.iter().map(String::as_str).collect();
        self.calls
            .borrow_mut()
            .push(format!("delete {} {}", view.pocket_key, names.join(",")));
        if let Some(stored) = self.managed.borrow_mut().get_mut(&view.pocket_key) {
            for key in keys {
                stored.remove(key);
            }
        }
        Ok(())
    }

    async fn user_secret_exists(
        &self,
        name: &str,
        _store: StoreType,
        _region: &str,
    ) -> Result<bool> {
        Ok(self.user.contains(name))
    }
}

/// stub server が受けた要求
#[derive(Debug, Clone)]
pub struct Request {
    pub method: String,
    /// query を含む
    pub path: String,
    /// header 名は小文字
    pub headers: Vec<(String, String)>,
    pub body: String,
}

impl Request {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.as_str())
    }

    pub fn json(&self) -> Json {
        serde_json::from_str(&self.body).expect("JSON の body")
    }

    /// `POST /path`
    pub fn line(&self) -> String {
        format!("{} {}", self.method, self.path)
    }
}

/// stub server の応答
pub struct Reply {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: String,
}

impl Reply {
    pub fn json(status: u16, body: Json) -> Self {
        Self {
            status,
            headers: Vec::new(),
            body: body.to_string(),
        }
    }

    pub fn ok(body: Json) -> Self {
        Self::json(200, body)
    }

    pub fn header(mut self, name: &str, value: &str) -> Self {
        self.headers.push((name.to_string(), value.to_string()));
        self
    }
}

/// localhost の HTTP server。受けた要求を記録し、`handler` が決めた応答を返す
/// (1 接続 1 要求)
pub struct StubServer {
    /// `http://127.0.0.1:<port>`
    pub url: String,
    requests: std::sync::Arc<std::sync::Mutex<Vec<Request>>>,
    stop: std::sync::Arc<std::sync::atomic::AtomicBool>,
    address: std::net::SocketAddr,
}

impl StubServer {
    pub fn start(mut handler: impl FnMut(&Request) -> Reply + Send + 'static) -> Self {
        use std::io::{BufRead as _, BufReader, Read as _, Write as _};
        use std::sync::atomic::Ordering;

        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind");
        let address = listener.local_addr().expect("addr");
        let requests = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let (recorded, stopping) = (requests.clone(), stop.clone());
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                if stopping.load(Ordering::SeqCst) {
                    break;
                }
                let Ok(mut stream) = stream else { continue };
                let mut reader = BufReader::new(stream.try_clone().expect("clone"));
                let mut first = String::new();
                if reader.read_line(&mut first).unwrap_or(0) == 0 {
                    continue;
                }
                let mut parts = first.split_whitespace();
                let method = parts.next().unwrap_or_default().to_string();
                let path = parts.next().unwrap_or_default().to_string();
                let mut headers = Vec::new();
                loop {
                    let mut line = String::new();
                    if reader.read_line(&mut line).unwrap_or(0) == 0 || line.trim().is_empty() {
                        break;
                    }
                    if let Some((key, value)) = line.split_once(':') {
                        headers.push((key.trim().to_lowercase(), value.trim().to_string()));
                    }
                }
                let length = headers
                    .iter()
                    .find(|(key, _)| key == "content-length")
                    .and_then(|(_, value)| value.parse::<usize>().ok())
                    .unwrap_or(0);
                let mut body = vec![0; length];
                reader.read_exact(&mut body).expect("body");
                let request = Request {
                    method,
                    path,
                    headers,
                    body: String::from_utf8(body).expect("utf-8"),
                };
                let reply = handler(&request);
                recorded.lock().expect("lock").push(request);
                let mut head = format!(
                    "HTTP/1.1 {} X\r\nContent-Length: {}\r\nConnection: close\r\n",
                    reply.status,
                    reply.body.len()
                );
                for (key, value) in &reply.headers {
                    head.push_str(&format!("{key}: {value}\r\n"));
                }
                head.push_str("\r\n");
                let _ = stream.write_all(head.as_bytes());
                let _ = stream.write_all(reply.body.as_bytes());
            }
        });
        Self {
            url: format!("http://{address}"),
            requests,
            stop,
            address,
        }
    }

    pub fn requests(&self) -> Vec<Request> {
        self.requests.lock().expect("lock").clone()
    }

    /// 受けた要求の `METHOD path` の並び
    pub fn lines(&self) -> Vec<String> {
        self.requests().iter().map(Request::line).collect()
    }

    /// 読み取り (GET) を除いた要求
    pub fn writes(&self) -> Vec<Request> {
        self.requests()
            .into_iter()
            .filter(|request| request.method != "GET")
            .collect()
    }
}

impl Drop for StubServer {
    fn drop(&mut self) {
        self.stop.store(true, std::sync::atomic::Ordering::SeqCst);
        // accept を起こして thread を終わらせる
        let _ = std::net::TcpStream::connect(self.address);
    }
}
