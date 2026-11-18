//! Aurora DSQL の cluster の手順 (Python 版 `resources/dsql.py` の deploy に関わる部分)。
//!
//! cluster の identifier は AWS が決めるので、`Name` タグで探す。endpoint は名前から
//! 導けないため、deploy が正準名の stored secret に公開する (migration などが読む)。
//!
//! Python 版との違い: cluster の検索と完了待ちで、「見つからない」以外の API error は
//! 握りつぶさずに止める (権限不足を「cluster 無し」と見なすと、2 つ目の cluster を作る)

use anyhow::Result;
use aws_config::SdkConfig;
use magic_pocket_settings::context::DsqlContext;
use serde_json::{json, Value as Json};

use super::poll::wait_until;
use super::stored_value::StoredValues;
use crate::aws_util::{has_code, regional_config, sdk_error};
use crate::stack::ops::WaitOptions;
use crate::status::sink::Sink;

#[derive(Debug, Clone, PartialEq)]
pub struct DsqlCluster {
    pub identifier: String,
    pub arn: String,
    /// `ACTIVE` など
    pub status: String,
}

pub trait DsqlApi {
    /// `Name` タグが `tag_name` の cluster
    async fn find(&self, region: &str, tag_name: &str) -> Result<Option<DsqlCluster>>;
    /// cluster の状態。無ければ `None`
    async fn status(&self, region: &str, identifier: &str) -> Result<Option<String>>;
    /// 作って identifier を返す (`args` は boto3 の引数の形)
    async fn create(&self, region: &str, args: &Json) -> Result<String>;
    async fn delete(&self, region: &str, identifier: &str) -> Result<()>;
}

/// 作成の引数 (Python の `Dsql.create`)
pub fn create_args(dsql: &DsqlContext) -> Json {
    json!({
        "deletionProtectionEnabled": dsql.deletion_protection,
        "tags": {"Name": dsql.tag_name},
    })
}

pub fn endpoint(dsql: &DsqlContext, identifier: &str) -> String {
    format!("{identifier}.dsql.{}.on.aws", dsql.region)
}

/// 作成・削除の完了待ち (Python 版は 600 秒を 5 秒おき)
pub fn default_wait() -> WaitOptions {
    WaitOptions::new(600, 5)
}

pub struct DsqlDeploy<'a, A, V> {
    pub api: &'a A,
    pub values: &'a V,
    pub wait: WaitOptions,
}

/// DSQL には組み込みの自動バックアップ (PITR / snapshot) が無い。backup の宣言が無い
/// stage では deploy のたびに知らせる
pub fn deploy_init(dsql: &DsqlContext, sink: &mut dyn Sink) {
    if dsql.backup.is_some() {
        return;
    }
    sink.warning("DSQL には自動バックアップ (PITR / スナップショット) がありません。");
    sink.warning(
        "  定期バックアップは [backup.dsql] を宣言してください。 \
         単発なら `pocket resource dsql backup` で取得できます。",
    );
}

impl<A: DsqlApi, V: StoredValues> DsqlDeploy<'_, A, V> {
    pub async fn create(&self, dsql: &DsqlContext, sink: &mut dyn Sink) -> Result<()> {
        sink.log(&format!("Creating DSQL cluster: {}", dsql.tag_name));
        let identifier = self.api.create(&dsql.region, &create_args(dsql)).await?;
        sink.log(&format!("Cluster ID: {identifier}"));
        sink.log("Waiting for DSQL cluster to become active...");
        wait_until(
            self.wait,
            "Waiting for cluster to be active",
            &format!(
                "Cluster did not become active within {} seconds",
                self.wait.timeout_secs
            ),
            || async {
                let status = self.api.status(&dsql.region, &identifier).await?;
                Ok(matches!(status.as_deref(), Some("ACTIVE" | "IDLE")))
            },
        )
        .await?;
        sink.success("DSQL cluster is now active.");
        sink.success(&format!("Endpoint: {}", endpoint(dsql, &identifier)));
        // 後続の resource が失敗して deploy の最後まで届かなくても記録が残るよう、
        // 作った直後にも公開する
        self.publish_endpoint(dsql, &identifier, sink).await
    }

    /// endpoint の公開を毎 deploy で合わせる (cluster の作り直しにも追従する)
    pub async fn ensure_post_deploy_state(
        &self,
        dsql: &DsqlContext,
        sink: &mut dyn Sink,
    ) -> Result<()> {
        match self.api.find(&dsql.region, &dsql.tag_name).await? {
            Some(cluster) => self.publish_endpoint(dsql, &cluster.identifier, sink).await,
            None => Ok(()),
        }
    }

    /// endpoint を正準名の stored secret に書く。値が同じなら書かない
    /// (SSM の version・SM の stage を増やさない)
    async fn publish_endpoint(
        &self,
        dsql: &DsqlContext,
        identifier: &str,
        sink: &mut dyn Sink,
    ) -> Result<()> {
        let name = &dsql.endpoint_secret_name;
        if name.is_empty() {
            return Ok(());
        }
        let value = endpoint(dsql, identifier);
        let store = dsql.endpoint_secret_store;
        if self
            .values
            .read(name, store, &dsql.region)
            .await?
            .as_deref()
            == Some(value.as_str())
        {
            return Ok(());
        }
        self.values.put(name, store, &value, &dsql.region).await?;
        sink.log(&format!("Published DSQL endpoint to {name}"));
        Ok(())
    }

    /// cluster を消し、公開した endpoint も消す
    pub async fn delete(&self, dsql: &DsqlContext, sink: &mut dyn Sink) -> Result<()> {
        let Some(cluster) = self.api.find(&dsql.region, &dsql.tag_name).await? else {
            return Ok(());
        };
        let identifier = &cluster.identifier;
        sink.log(&format!("Deleting DSQL cluster: {identifier}"));
        self.api.delete(&dsql.region, identifier).await?;
        sink.log("Waiting for DSQL cluster deletion...");
        wait_until(
            self.wait,
            "Waiting for cluster deletion",
            &format!(
                "Cluster not deleted within {} seconds",
                self.wait.timeout_secs
            ),
            || async { Ok(self.api.status(&dsql.region, identifier).await?.is_none()) },
        )
        .await?;
        sink.success("DSQL cluster was deleted.");
        let name = &dsql.endpoint_secret_name;
        if name.is_empty() {
            return Ok(());
        }
        self.values
            .delete(name, dsql.endpoint_secret_store, &dsql.region)
            .await?;
        sink.log(&format!("Removed published DSQL endpoint: {name}"));
        Ok(())
    }
}

/// `Name` タグが一致する最初の cluster (全ページをたどる)。
///
/// 一覧の後で消えた cluster は飛ばす。他の error は止める
pub async fn find_cluster(
    sdk: &SdkConfig,
    region: &str,
    tag_name: &str,
) -> Result<Option<DsqlCluster>> {
    let client = aws_sdk_dsql::Client::new(&regional_config(sdk, region));
    let mut pages = client.list_clusters().into_paginator().send();
    while let Some(page) = pages.next().await {
        let page = page.map_err(|e| sdk_error("dsql list_clusters", &e))?;
        for summary in page.clusters() {
            let identifier = summary.identifier();
            let detail = match client.get_cluster().identifier(identifier).send().await {
                Ok(detail) => detail,
                Err(e) if has_code(&e, &["ResourceNotFoundException"]) => continue,
                Err(e) => return Err(sdk_error("dsql get_cluster", &e)),
            };
            let tags = client
                .list_tags_for_resource()
                .resource_arn(detail.arn())
                .send()
                .await;
            let tags = match tags {
                Ok(tags) => tags,
                Err(e) if has_code(&e, &["ResourceNotFoundException"]) => continue,
                Err(e) => return Err(sdk_error("dsql list_tags_for_resource", &e)),
            };
            let name = tags.tags().and_then(|tags| tags.get("Name"));
            if name.map(String::as_str) == Some(tag_name) {
                return Ok(Some(DsqlCluster {
                    identifier: detail.identifier().to_string(),
                    arn: detail.arn().to_string(),
                    status: detail.status().as_str().to_string(),
                }));
            }
        }
    }
    Ok(None)
}

/// AWS に対する [`DsqlApi`]
pub struct AwsDsql<'a> {
    pub sdk: &'a SdkConfig,
}

impl AwsDsql<'_> {
    fn client(&self, region: &str) -> aws_sdk_dsql::Client {
        aws_sdk_dsql::Client::new(&regional_config(self.sdk, region))
    }
}

impl DsqlApi for AwsDsql<'_> {
    async fn find(&self, region: &str, tag_name: &str) -> Result<Option<DsqlCluster>> {
        find_cluster(self.sdk, region, tag_name).await
    }

    async fn status(&self, region: &str, identifier: &str) -> Result<Option<String>> {
        let response = self
            .client(region)
            .get_cluster()
            .identifier(identifier)
            .send()
            .await;
        match response {
            Ok(detail) => Ok(Some(detail.status().as_str().to_string())),
            Err(e) if has_code(&e, &["ResourceNotFoundException"]) => Ok(None),
            Err(e) => Err(sdk_error("dsql get_cluster", &e)),
        }
    }

    async fn create(&self, region: &str, args: &Json) -> Result<String> {
        let mut request = self
            .client(region)
            .create_cluster()
            .set_deletion_protection_enabled(args["deletionProtectionEnabled"].as_bool());
        for (key, value) in args["tags"].as_object().into_iter().flatten() {
            request = request.tags(key, value.as_str().unwrap_or_default());
        }
        let created = request
            .send()
            .await
            .map_err(|e| sdk_error("dsql create_cluster", &e))?;
        Ok(created.identifier().to_string())
    }

    async fn delete(&self, region: &str, identifier: &str) -> Result<()> {
        self.client(region)
            .delete_cluster()
            .identifier(identifier)
            .send()
            .await
            .map_err(|e| sdk_error("dsql delete_cluster", &e))?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;

    use anyhow::bail;

    use super::*;
    use crate::deploy::poll::testing::{fast, run};
    use crate::deploy::stored_value::fake::FakeStoredValues;
    use crate::stack::testing::{context, read_json, repo_root, vector_context, vector_dirs};
    use crate::status::sink::{Recorded, Style};

    #[derive(Default)]
    struct FakeDsql {
        cluster: RefCell<Option<DsqlCluster>>,
        /// 作成後の状態 (既定は ACTIVE)
        status_after_create: Option<&'static str>,
        /// 状態の取得が権限不足で失敗する
        denied: bool,
        calls: RefCell<Vec<String>>,
    }

    impl DsqlApi for FakeDsql {
        async fn find(&self, _region: &str, _tag_name: &str) -> Result<Option<DsqlCluster>> {
            Ok(self.cluster.borrow().clone())
        }

        async fn status(&self, _region: &str, _identifier: &str) -> Result<Option<String>> {
            if self.denied {
                bail!("dsql get_cluster: AccessDeniedException: denied");
            }
            Ok(self.cluster.borrow().as_ref().map(|c| c.status.clone()))
        }

        async fn create(&self, _region: &str, args: &Json) -> Result<String> {
            self.calls.borrow_mut().push(format!("create {args}"));
            *self.cluster.borrow_mut() = Some(DsqlCluster {
                identifier: "abc123".to_string(),
                arn: "arn:dsql:abc123".to_string(),
                status: self.status_after_create.unwrap_or("ACTIVE").to_string(),
            });
            Ok("abc123".to_string())
        }

        async fn delete(&self, _region: &str, identifier: &str) -> Result<()> {
            self.calls.borrow_mut().push(format!("delete {identifier}"));
            *self.cluster.borrow_mut() = None;
            Ok(())
        }
    }

    fn dsql() -> DsqlContext {
        vector_context("toml_vectors/db_backup.dev")
            .dsql
            .expect("dsql")
    }

    fn deploy<'a>(
        api: &'a FakeDsql,
        values: &'a FakeStoredValues,
    ) -> DsqlDeploy<'a, FakeDsql, FakeStoredValues> {
        DsqlDeploy {
            api,
            values,
            wait: fast(3),
        }
    }

    #[test]
    fn test_documents_match_python() {
        let root = repo_root();
        let mut count = 0;
        for dir in vector_dirs() {
            let path = dir.join("documents.json");
            if !path.is_file() {
                continue;
            }
            let want = &read_json(&path)["dsql"];
            if want.is_null() {
                continue;
            }
            let ctx = context(&root, &read_json(&dir.join("manifest.json")));
            let dsql = ctx.dsql.as_ref().expect("dsql");
            assert_eq!(
                create_args(dsql),
                want["create_cluster"],
                "{}",
                dir.display()
            );
            assert_eq!(
                endpoint(dsql, want["identifier"].as_str().expect("identifier")),
                want["endpoint"].as_str().expect("endpoint")
            );
            count += 1;
        }
        assert!(count >= 4, "vector が少なすぎます: {count}");
    }

    #[test]
    fn test_create_waits_then_publishes_endpoint() {
        let dsql = dsql();
        let (api, values) = (FakeDsql::default(), FakeStoredValues::default());
        let mut sink = Recorded::default();
        run(deploy(&api, &values).create(&dsql, &mut sink)).expect("create");
        let store = dsql.endpoint_secret_store;
        let name = &dsql.endpoint_secret_name;
        assert_eq!(
            values.get(store, name).as_deref(),
            Some(endpoint(&dsql, "abc123").as_str())
        );
        // 値が同じなら書かない
        run(deploy(&api, &values).ensure_post_deploy_state(&dsql, &mut sink)).expect("post");
        assert_eq!(values.calls().len(), 1);
        // 値が違えば書き直す (cluster の作り直し)
        values.set(store, name, "old.dsql.example");
        run(deploy(&api, &values).ensure_post_deploy_state(&dsql, &mut sink)).expect("post");
        assert_eq!(values.calls().len(), 2);
    }

    #[test]
    fn test_create_stops_when_cluster_never_becomes_active_or_status_fails() {
        let dsql = dsql();
        let api = FakeDsql {
            status_after_create: Some("CREATING"),
            ..Default::default()
        };
        let values = FakeStoredValues::default();
        let mut sink = Recorded::default();
        let error = run(deploy(&api, &values).create(&dsql, &mut sink)).expect_err("timeout");
        assert!(error
            .to_string()
            .starts_with("Cluster did not become active"));
        assert!(values.calls().is_empty(), "active になるまで公開しない");

        // 権限不足は「まだ active でない」と見なさずに止める
        let api = FakeDsql {
            denied: true,
            ..Default::default()
        };
        let error = run(deploy(&api, &values).create(&dsql, &mut sink)).expect_err("denied");
        assert!(error.to_string().contains("AccessDeniedException"));
    }

    #[test]
    fn test_post_state_without_cluster_does_nothing() {
        let dsql = dsql();
        let (api, values) = (FakeDsql::default(), FakeStoredValues::default());
        let mut sink = Recorded::default();
        run(deploy(&api, &values).ensure_post_deploy_state(&dsql, &mut sink)).expect("post");
        assert!(values.calls().is_empty());
    }

    #[test]
    fn test_delete_removes_cluster_then_published_endpoint() {
        let dsql = dsql();
        let (api, values) = (FakeDsql::default(), FakeStoredValues::default());
        let mut sink = Recorded::default();
        run(deploy(&api, &values).create(&dsql, &mut sink)).expect("create");
        run(deploy(&api, &values).delete(&dsql, &mut sink)).expect("delete");
        assert_eq!(
            api.calls.borrow().last().map(String::as_str),
            Some("delete abc123")
        );
        let store = dsql.endpoint_secret_store;
        assert_eq!(values.get(store, &dsql.endpoint_secret_name), None);
        // cluster が無ければ何もしない
        let before = values.calls().len();
        run(deploy(&api, &values).delete(&dsql, &mut sink)).expect("noop");
        assert_eq!(values.calls().len(), before);
    }

    #[test]
    fn test_deploy_init_warns_without_backup() {
        let mut dsql = vector_context("toml_vectors/db_backup.prod")
            .dsql
            .expect("dsql");
        let mut sink = Recorded::default();
        deploy_init(&dsql, &mut sink);
        assert!(sink.0.is_empty(), "backup の宣言があれば何も出さない");
        dsql.backup = None;
        deploy_init(&dsql, &mut sink);
        assert_eq!(sink.0.len(), 2);
        assert!(sink.0.iter().all(|r| r.1 == Style::Warning));
    }

    #[test]
    fn test_default_wait_matches_python() {
        let wait = default_wait();
        assert_eq!((wait.attempts, wait.interval.as_secs()), (120, 5));
    }
}
