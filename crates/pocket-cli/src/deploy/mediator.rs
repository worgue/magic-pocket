//! managed secret の用意 (Python 版 `pocket_cli/mediator.py` と `migrations.py` の ensure)。
//!
//! deploy は container の stack を作る前に [`ensure_pocket_managed_secrets`] を呼ぶ:
//!
//! 1. 旧 project 共有パスの値を container store へ引き継ぐ (0.29.0 の移行)
//! 2. store に無い managed secret を生成する (shared → container 名順)
//! 3. managed の定義に無い key を store から消す
//! 4. type 付きの user secret (stored mode) が provision 済みか確かめる
//!
//! 生成後は secret の ARN が変わりうるので、Context を作り直してから template を描く
//! (Python 版は view の cache を捨てる)。戻り値の [`SecretsReady`] がその Context を持つ。

use std::collections::BTreeSet;

use anyhow::{anyhow, bail, Result};
use magic_pocket_settings::context::{Context, SecretsContext};
use magic_pocket_settings::model::{ManagedSecretSpec, ManagedSecretType};
use serde::Serialize;
use serde_json::Value as Json;

use super::generate;
use super::secret_store::{ManagedSecrets, SecretStore};
use crate::status::sink::Sink;

/// 既に在る secret の扱い (Python 版の `ErrorLevel`)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorLevel {
    Ignore,
    Warning,
    Raise,
}

/// managed secret の用意が済んだ後に作り直した Context
pub struct SecretsReady(pub Context);

pub struct Mediator<'a, S> {
    pub ctx: &'a Context,
    pub store: &'a S,
}

impl<S: SecretStore> Mediator<'_, S> {
    /// deploy 前の用意。`reload` は Context の作り直し (secret の ARN を引き直す)
    pub async fn ensure_pocket_managed_secrets(
        &self,
        sink: &mut dyn Sink,
        reload: impl FnOnce() -> Result<Context>,
    ) -> Result<SecretsReady> {
        self.create_pocket_managed_secrets(sink, ErrorLevel::Ignore)
            .await?;
        self.cleanup_orphaned_secrets(sink).await?;
        if let Some(shared) = &self.ctx.secrets {
            self.verify_provisioned(shared).await?;
        }
        Ok(SecretsReady(reload()?))
    }

    pub async fn create_pocket_managed_secrets(
        &self,
        sink: &mut dyn Sink,
        exists: ErrorLevel,
    ) -> Result<()> {
        // 生成の前に旧配置からの引き継ぎを済ませる (引き継ぐ前に再生成しない)
        self.inherit_legacy_secrets(sink).await?;
        if let Some(shared) = &self.ctx.secrets {
            self.create_store_secrets(sink, shared, exists).await?;
        }
        for view in self.container_views() {
            self.create_store_secrets(sink, view, exists).await?;
        }
        Ok(())
    }

    /// container store の view (container 名の昇順)
    fn container_views(&self) -> Vec<&SecretsContext> {
        let mut containers: Vec<_> = self.ctx.container.iter().collect();
        containers.sort_by(|a, b| a.0.cmp(b.0));
        containers
            .into_iter()
            .filter_map(|(_, c)| c.secrets.as_ref())
            .collect()
    }

    /// 0.29.0 の移行: 旧 project 共有パスにある値を container store へコピーする。
    ///
    /// container store に無く旧パスにある key は、再生成せず値を引き継ぐ (SECRET_KEY を
    /// 作り直すと session / 署名 cookie が無効になる)。shared 宣言の現役 key は移行の
    /// 残骸ではないので引き継ぎ元にしない
    async fn inherit_legacy_secrets(&self, sink: &mut dyn Sink) -> Result<()> {
        let Some(shared) = &self.ctx.secrets else {
            return Ok(());
        };
        let legacy = self.store.read(shared).await?;
        if legacy.is_empty() {
            return Ok(());
        }
        for view in self.container_views() {
            let stored = self.store.read(view).await?;
            let inherited: Vec<(&String, &Json)> = view
                .managed
                .keys()
                .filter(|key| !stored.contains_key(*key) && !shared.managed.contains_key(key))
                .filter_map(|key| legacy.get(key).map(|value| (key, value)))
                .collect();
            if inherited.is_empty() {
                continue;
            }
            let mut names: Vec<&String> = inherited.iter().map(|(key, _)| *key).collect();
            names.sort();
            for key in names {
                sink.log(&format!(
                    "secret '{key}' を旧 project 共有パスから container store ({}) へ\
                     引き継ぎます (値は再生成しません)。",
                    view.pocket_key
                ));
            }
            let mut merged = stored.clone();
            for (key, value) in inherited {
                merged.insert(key.clone(), value.clone());
            }
            self.store.update(view, &merged).await?;
        }
        Ok(())
    }

    /// store view 1 つ分の managed secret を生成する (無いものだけ)
    async fn create_store_secrets(
        &self,
        sink: &mut dyn Sink,
        view: &SecretsContext,
        exists: ErrorLevel,
    ) -> Result<()> {
        let stored = self.store.read(view).await?;
        let mut generated = ManagedSecrets::new();
        for (key, spec) in view.managed.iter() {
            if stored.contains_key(key) {
                let message = format!(
                    "{key} is already created. To rotate, run `pocket resource \
                     container secrets delete-pocket-managed` and then \
                     `create-pocket-managed`."
                );
                conditional_error(sink, exists, message)?;
                continue;
            }
            generated.insert(key.clone(), self.generate_secret(spec)?);
        }
        if !generated.is_empty() {
            let mut merged = stored;
            for (key, value) in generated {
                merged.insert(key, value);
            }
            self.store.update(view, &merged).await?;
        }
        Ok(())
    }

    /// managed の定義に無い key を store から消す。
    ///
    /// shared store では、container store へ移った (shared でない) 宣言済みの key を
    /// 残す。旧 stack の Lambda が切り替えまで旧パスを読み続けるためで、その残骸は
    /// deploy 成功後の掃除 (確認付き) が消す
    async fn cleanup_orphaned_secrets(&self, sink: &mut dyn Sink) -> Result<()> {
        if let Some(shared) = &self.ctx.secrets {
            let mut expected: BTreeSet<&String> = shared.managed.keys().collect();
            for (_, container) in self.ctx.container.iter() {
                if let Some(view) = &container.secrets {
                    expected.extend(view.managed.keys());
                }
            }
            self.delete_orphans(sink, shared, &expected).await?;
        }
        for (_, container) in self.ctx.container.iter() {
            if let Some(view) = &container.secrets {
                let expected: BTreeSet<&String> = view.managed.keys().collect();
                self.delete_orphans(sink, view, &expected).await?;
            }
        }
        Ok(())
    }

    async fn delete_orphans(
        &self,
        sink: &mut dyn Sink,
        view: &SecretsContext,
        expected: &BTreeSet<&String>,
    ) -> Result<()> {
        let stored = self.store.read(view).await?;
        let orphaned: BTreeSet<String> = stored
            .keys()
            .filter(|key| !expected.contains(key))
            .cloned()
            .collect();
        if orphaned.is_empty() {
            return Ok(());
        }
        let names: Vec<&str> = orphaned.iter().map(String::as_str).collect();
        sink.warning(&format!(
            "managed 定義にないシークレットを削除します ({}): {}",
            view.pocket_key,
            names.join(", ")
        ));
        self.store.delete_keys(view, &orphaned).await
    }

    /// type 付きの user secret (stored mode) が store にあるか確かめる。
    ///
    /// pocket は値を生成しないので、無いまま deploy すると runtime まで気付けない
    async fn verify_provisioned(&self, view: &SecretsContext) -> Result<()> {
        let mut missing = Vec::new();
        for (key, spec) in view.user.iter() {
            let (Some(secret_type), Some(name)) = (&spec.r#type, &spec.name) else {
                continue;
            };
            let store = spec.store.unwrap_or(view.store);
            if !self
                .store
                .user_secret_exists(name, store, &view.region)
                .await?
            {
                missing.push(format!(
                    "  - {key} (type={}, store={}): {name}",
                    serde_name(secret_type),
                    serde_name(&store)
                ));
            }
        }
        if missing.is_empty() {
            return Ok(());
        }
        bail!(
            "stored mode の user secret が見つかりません。\
             deploy 前に下記の secret を provision してください \
             (値は接続 URL):\n{}",
            missing.join("\n")
        )
    }

    fn generate_secret(&self, spec: &ManagedSecretSpec) -> Result<Json> {
        let text = Json::String;
        Ok(match spec.r#type {
            ManagedSecretType::AutoDatabaseUrl => text(self.auto_database_url()?),
            ManagedSecretType::Password => text(generate::password(&spec.options)?),
            ManagedSecretType::RdsDatabaseUrl => text(self.rds_database_url()?),
            ManagedSecretType::RsaPemBase64 | ManagedSecretType::CloudfrontSigningKey => {
                generate::rsa_pem()?
            }
            ManagedSecretType::SpaTokenSecret
            | ManagedSecretType::OriginVerifySecret
            | ManagedSecretType::WafAllowSecret => text(generate::token_hex(32)?),
            ManagedSecretType::BasicAuthCredential => {
                text(generate::basic_auth_credential(&spec.options)?)
            }
        })
    }

    /// RDS の DATABASE_URL は runtime が組み立てるので、deploy 時は目印の値を入れる
    fn rds_database_url(&self) -> Result<String> {
        if self.ctx.rds.is_none() {
            bail!("rds is not configured. Please set rds in pocket.toml");
        }
        Ok("__rds_runtime__".to_string())
    }

    /// `[rds]` があれば rds_database_url と同じ値。Neon / TiDB の接続 URL は deploy では
    /// 作らない (`pocket resource <db> store-url` が保存した user secret を使う)
    fn auto_database_url(&self) -> Result<String> {
        if self.ctx.rds.is_some() {
            return self.rds_database_url();
        }
        bail!(
            "auto_database_url が使えるのは [rds] だけです。Neon / TiDB は \
             [container.<name>.secrets.user] に type で宣言し、\
             `pocket resource <db> store-url` で接続 URL を保存してください。"
        )
    }
}

fn conditional_error(sink: &mut dyn Sink, level: ErrorLevel, message: String) -> Result<()> {
    match level {
        ErrorLevel::Ignore => Ok(()),
        ErrorLevel::Warning => {
            sink.warning(&message);
            Ok(())
        }
        ErrorLevel::Raise => Err(anyhow!(message)),
    }
}

/// enum の pocket.toml 上の名前 (`neon_database_url` / `sm` など)
fn serde_name<T: Serialize>(value: &T) -> String {
    match serde_json::to_value(value) {
        Ok(Json::String(name)) => name,
        _ => String::new(),
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;
    use crate::deploy::testing::FakeStore;
    use crate::stack::testing::context_from_toml;
    use crate::status::sink::{Stream, Style};

    #[derive(Default)]
    struct Recorder(Vec<(Style, String)>);

    impl Sink for Recorder {
        fn emit(&mut self, stream: Stream, style: Style, text: &str) {
            assert_eq!(stream, Stream::Stderr);
            self.0.push((style, text.to_string()));
        }
    }

    impl Recorder {
        fn texts(&self, style: Style) -> Vec<&str> {
            self.0
                .iter()
                .filter(|(s, _)| *s == style)
                .map(|(_, text)| text.as_str())
                .collect()
        }
    }

    fn run<T>(future: impl std::future::Future<Output = T>) -> T {
        tokio::runtime::Builder::new_current_thread()
            .build()
            .expect("runtime")
            .block_on(future)
    }

    const TWO_CONTAINERS: &str = r#"
[general]
region = "ap-southeast-1"
project_name = "testprj"
stages = ["prod"]

[container.web]
dockerfile_path = "Dockerfile"

[container.web.secrets.managed]
SECRET_KEY = { type = "password", options = { length = 50 }, shared = true }
WEB_ONLY = { type = "spa_token_secret" }

[container.api]
dockerfile_path = "Dockerfile"

[container.api.secrets.managed]
SECRET_KEY = { type = "password", options = { length = 50 }, shared = true }
API_KEY = { type = "rsa_pem_base64" }
"#;

    const SHARED: &str = "prod-testprj-pocket";
    const WEB: &str = "prod-testprj-web-pocket";
    const API: &str = "prod-testprj-api-pocket";

    fn ensure(ctx: &Context, store: &FakeStore) -> (Result<SecretsReady>, Recorder) {
        let mut sink = Recorder::default();
        let mediator = Mediator { ctx, store };
        let result = run(mediator.ensure_pocket_managed_secrets(&mut sink, || Ok(ctx.clone())));
        (result, sink)
    }

    #[test]
    fn test_fixture_views() {
        // 下のテストが前提にする view の構成
        let ctx = context_from_toml(TWO_CONTAINERS, "prod");
        let mut listed = views(&ctx);
        listed.sort();
        assert_eq!(
            listed,
            vec![
                (API.to_string(), vec!["API_KEY".to_string()]),
                (SHARED.to_string(), vec!["SECRET_KEY".to_string()]),
                (WEB.to_string(), vec!["WEB_ONLY".to_string()]),
            ]
        );
    }

    #[test]
    fn test_generates_missing_secrets_shared_first_then_container_name_order() {
        let ctx = context_from_toml(TWO_CONTAINERS, "prod");
        let store = FakeStore::default();
        let (result, sink) = ensure(&ctx, &store);
        result.expect("ensure");
        assert_eq!(
            *store.calls.borrow(),
            vec![
                format!("update {SHARED} SECRET_KEY"),
                format!("update {API} API_KEY"),
                format!("update {WEB} WEB_ONLY"),
            ]
        );
        let secret_key = store.secrets(SHARED)["SECRET_KEY"]
            .as_str()
            .unwrap()
            .to_string();
        assert_eq!(secret_key.chars().count(), 50);
        assert_eq!(store.secrets(WEB)["WEB_ONLY"].as_str().unwrap().len(), 64);
        let rsa = &store.secrets(API)["API_KEY"];
        assert!(rsa["pem"].is_string() && rsa["pub"].is_string(), "{rsa}");
        assert!(sink.0.is_empty(), "{:?}", sink.0);
    }

    #[test]
    fn test_existing_secrets_are_kept_and_nothing_is_written() {
        let ctx = context_from_toml(TWO_CONTAINERS, "prod");
        let store = FakeStore::default()
            .with(SHARED, json!({"SECRET_KEY": "keep"}))
            .with(WEB, json!({"WEB_ONLY": "keep-web"}))
            .with(API, json!({"API_KEY": {"pem": "a", "pub": "b"}}));
        let (result, sink) = ensure(&ctx, &store);
        result.expect("ensure");
        assert!(store.calls.borrow().is_empty(), "{:?}", store.calls);
        assert_eq!(store.secrets(SHARED)["SECRET_KEY"], "keep");
        // deploy (exists = ignore) は既存の key について何も出さない
        assert!(sink.0.is_empty(), "{:?}", sink.0);
    }

    #[test]
    fn test_create_warns_or_raises_on_existing_key() {
        let ctx = context_from_toml(TWO_CONTAINERS, "prod");
        let store = FakeStore::default().with(SHARED, json!({"SECRET_KEY": "keep"}));
        let mediator = Mediator {
            ctx: &ctx,
            store: &store,
        };
        let mut sink = Recorder::default();
        run(mediator.create_pocket_managed_secrets(&mut sink, ErrorLevel::Warning))
            .expect("create");
        assert_eq!(
            sink.texts(Style::Warning),
            vec![
                "SECRET_KEY is already created. To rotate, run `pocket resource container \
                 secrets delete-pocket-managed` and then `create-pocket-managed`."
            ]
        );
        let error =
            run(mediator.create_pocket_managed_secrets(&mut sink, ErrorLevel::Raise)).unwrap_err();
        assert!(error
            .to_string()
            .starts_with("SECRET_KEY is already created."));
    }

    #[test]
    fn test_inherits_legacy_values_instead_of_regenerating() {
        let ctx = context_from_toml(TWO_CONTAINERS, "prod");
        // 0.29.0 より前は全部が project 共有パスにあった
        let store = FakeStore::default().with(
            SHARED,
            json!({"SECRET_KEY": "shared", "WEB_ONLY": "old-web", "API_KEY": {"pem": "p", "pub": "q"}}),
        );
        let (result, sink) = ensure(&ctx, &store);
        result.expect("ensure");
        assert_eq!(store.secrets(WEB)["WEB_ONLY"], "old-web");
        assert_eq!(
            store.secrets(API)["API_KEY"],
            json!({"pem": "p", "pub": "q"})
        );
        // shared の現役 key は container store へコピーしない
        assert_eq!(store.keys(WEB), vec!["WEB_ONLY"]);
        assert_eq!(
            sink.texts(Style::Log),
            vec![
                format!(
                    "secret 'API_KEY' を旧 project 共有パスから container store ({API}) へ\
                     引き継ぎます (値は再生成しません)。"
                ),
                format!(
                    "secret 'WEB_ONLY' を旧 project 共有パスから container store ({WEB}) へ\
                     引き継ぎます (値は再生成しません)。"
                ),
            ]
        );
        // 旧パスの残骸 (container が宣言している key) は shared store に残す
        assert_eq!(
            store.keys(SHARED),
            vec!["SECRET_KEY", "WEB_ONLY", "API_KEY"]
        );
        assert!(sink.texts(Style::Warning).is_empty());
    }

    #[test]
    fn test_orphans_are_deleted_key_by_key() {
        let ctx = context_from_toml(TWO_CONTAINERS, "prod");
        let store = FakeStore::default()
            .with(
                SHARED,
                json!({"SECRET_KEY": "s", "GONE": "x", "ALSO_GONE": "y"}),
            )
            .with(WEB, json!({"WEB_ONLY": "w", "OLD": "z"}))
            .with(API, json!({"API_KEY": {"pem": "a", "pub": "b"}}));
        let (result, sink) = ensure(&ctx, &store);
        result.expect("ensure");
        assert_eq!(
            *store.calls.borrow(),
            vec![
                format!("delete {SHARED} ALSO_GONE,GONE"),
                format!("delete {WEB} OLD"),
            ]
        );
        assert_eq!(
            sink.texts(Style::Warning),
            vec![
                format!("managed 定義にないシークレットを削除します ({SHARED}): ALSO_GONE, GONE"),
                format!("managed 定義にないシークレットを削除します ({WEB}): OLD"),
            ]
        );
        assert_eq!(store.keys(SHARED), vec!["SECRET_KEY"]);
    }

    fn database_toml(extra: &str, managed: &str) -> String {
        format!(
            r#"
[general]
region = "ap-southeast-1"
project_name = "testprj"
stages = ["prod"]
{extra}
[container.web]
dockerfile_path = "Dockerfile"

[container.web.secrets.managed]
{managed}
"#
        )
    }

    const NEON: &str = "[neon]\nproject_name = \"p\"\n";

    #[test]
    fn test_auto_database_url_is_rds_only() {
        let toml = database_toml(NEON, "DATABASE_URL = { type = \"auto_database_url\" }");
        let ctx = context_from_toml(&toml, "prod");
        let store = FakeStore::default();
        let (result, _) = ensure(&ctx, &store);
        assert_eq!(
            result.err().expect("失敗するはず").to_string(),
            "auto_database_url が使えるのは [rds] だけです。Neon / TiDB は \
             [container.<name>.secrets.user] に type で宣言し、\
             `pocket resource <db> store-url` で接続 URL を保存してください。"
        );
        assert!(store.calls.borrow().is_empty());
    }

    #[test]
    fn test_user_secret_must_be_provisioned() {
        let toml = database_toml(
            NEON,
            "SECRET_KEY = { type = \"password\" }\n\n[container.web.secrets.user]\n\
             DATABASE_URL = { type = \"neon_database_url\" }",
        );
        let ctx = context_from_toml(&toml, "prod");
        let name = ctx
            .secrets
            .as_ref()
            .expect("shared view")
            .user
            .get("DATABASE_URL")
            .and_then(|spec| spec.name.clone())
            .expect("正準名");
        let (result, _) = ensure(&ctx, &FakeStore::default());
        assert_eq!(
            result.err().expect("失敗するはず").to_string(),
            format!(
                "stored mode の user secret が見つかりません。\
                 deploy 前に下記の secret を provision してください (値は接続 URL):\n  \
                 - DATABASE_URL (type=neon_database_url, store=sm): {name}"
            )
        );
        let store = FakeStore {
            user: BTreeSet::from([name]),
            ..Default::default()
        };
        let (result, _) = ensure(&ctx, &store);
        result.expect("provision 済みなら通る");
    }

    /// vector の Context の view と宣言を一覧する (fixture の前提を assert で固定する)
    fn views(ctx: &Context) -> Vec<(String, Vec<String>)> {
        let mut out = Vec::new();
        if let Some(shared) = &ctx.secrets {
            out.push((
                shared.pocket_key.clone(),
                shared.managed.keys().cloned().collect(),
            ));
        }
        for (_, c) in ctx.container.iter() {
            if let Some(view) = &c.secrets {
                out.push((
                    view.pocket_key.clone(),
                    view.managed.keys().cloned().collect(),
                ));
            }
        }
        out
    }
}
