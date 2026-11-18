//! EFS の file system の確保と削除 (Python 版 `resources/aws/efs.py`)。
//!
//! file system は `Name` タグで探す。作った後は lifecycle policy (30 日で IA へ、
//! 1 回の access で標準へ戻す) を設定する。

use anyhow::{anyhow, bail, Result};
use aws_config::SdkConfig;
use aws_sdk_efs::types::{
    LifecyclePolicy, Tag, TransitionToIaRules, TransitionToPrimaryStorageClassRules,
};
use magic_pocket_settings::context::EfsContext;
use serde_json::{json, Map, Value as Json};

use super::generate::token_hex;
use super::poll::wait_until;
use crate::aws_util::{regional_config, sdk_error};
use crate::stack::ops::{ResourceStatus, WaitOptions};

/// `Name` タグで見つけた file system
#[derive(Debug, Clone, PartialEq)]
pub struct FileSystem {
    pub id: String,
    /// `available` など
    pub life_cycle_state: String,
}

pub trait EfsApi {
    /// `Name` タグが `name` の file system。無ければ `None`
    async fn find(&self, region: &str, name: &str) -> Result<Option<FileSystem>>;
    /// 作って id を返す (`args` は boto3 の引数の形)
    async fn create(&self, region: &str, args: &Json) -> Result<String>;
    /// 今の lifecycle policy (boto3 が返す形)
    async fn lifecycle_policies(&self, region: &str, id: &str) -> Result<Json>;
    async fn put_lifecycle_policies(&self, region: &str, id: &str, policies: &Json) -> Result<()>;
    async fn delete(&self, region: &str, id: &str) -> Result<()>;
    /// mount target の ENI の id
    async fn mount_target_network_interface(
        &self,
        region: &str,
        mount_target_id: &str,
    ) -> Result<Option<String>>;
}

/// 作成の引数 (`CreationToken` を除く。Python の `Efs.create`)
pub fn create_args(efs: &EfsContext) -> Json {
    json!({
        "Encrypted": true,
        "Backup": true,
        "Tags": [{"Key": "Name", "Value": efs.name}],
    })
}

/// 望む lifecycle policy (Python の `lifecycle_policies_should_be`)
pub fn lifecycle_policies_should_be() -> Json {
    json!([
        {"TransitionToIA": "AFTER_30_DAYS"},
        {"TransitionToPrimaryStorageClass": "AFTER_1_ACCESS"},
    ])
}

/// 作成後に lifecycle の設定待ちになるまでの待機 (Python 版は 300 秒を 3 秒おき)
pub fn default_wait() -> WaitOptions {
    WaitOptions::new(300, 3)
}

pub async fn status(api: &impl EfsApi, efs: &EfsContext) -> Result<ResourceStatus> {
    let Some(found) = api.find(&efs.region, &efs.name).await? else {
        return Ok(ResourceStatus::NoExist);
    };
    if found.life_cycle_state != "available" {
        return Ok(ResourceStatus::Progress);
    }
    if api.lifecycle_policies(&efs.region, &found.id).await? != lifecycle_policies_should_be() {
        return Ok(ResourceStatus::RequireUpdate);
    }
    Ok(ResourceStatus::Completed)
}

/// 無ければ作る (在れば何もしない)
pub async fn ensure_exists(api: &impl EfsApi, efs: &EfsContext, wait: WaitOptions) -> Result<()> {
    if api.find(&efs.region, &efs.name).await?.is_some() {
        return Ok(());
    }
    create(api, efs, wait).await
}

pub async fn create(api: &impl EfsApi, efs: &EfsContext, wait: WaitOptions) -> Result<()> {
    let mut args = create_args(efs);
    args["CreationToken"] = Json::from(token_hex(16)?);
    let id = api.create(&efs.region, &args).await?;
    println!("Created EFS: {id}");
    let target = ResourceStatus::RequireUpdate;
    wait_until(
        wait,
        &format!("Waiting for efs status to be {target}"),
        &format!(
            "EFS status did not become {target} in {} seconds",
            wait.timeout_secs
        ),
        || async { Ok(status(api, efs).await? == target) },
    )
    .await?;
    let desired = lifecycle_policies_should_be();
    if api.lifecycle_policies(&efs.region, &id).await? != desired {
        api.put_lifecycle_policies(&efs.region, &id, &desired)
            .await?;
    }
    Ok(())
}

/// 在れば消す (消したら `true`)
pub async fn delete_if_exists(api: &impl EfsApi, efs: &EfsContext) -> Result<bool> {
    let Some(found) = api.find(&efs.region, &efs.name).await? else {
        return Ok(false);
    };
    api.delete(&efs.region, &found.id).await?;
    Ok(true)
}

/// AWS に対する [`EfsApi`]
pub struct AwsEfs<'a> {
    pub sdk: &'a SdkConfig,
}

impl AwsEfs<'_> {
    fn client(&self, region: &str) -> aws_sdk_efs::Client {
        aws_sdk_efs::Client::new(&regional_config(self.sdk, region))
    }
}

fn policy_json(policy: &LifecyclePolicy) -> Json {
    let mut item = Map::new();
    if let Some(rule) = policy.transition_to_ia() {
        item.insert("TransitionToIA".to_string(), json!(rule.as_str()));
    }
    if let Some(rule) = policy.transition_to_primary_storage_class() {
        item.insert(
            "TransitionToPrimaryStorageClass".to_string(),
            json!(rule.as_str()),
        );
    }
    if let Some(rule) = policy.transition_to_archive() {
        item.insert("TransitionToArchive".to_string(), json!(rule.as_str()));
    }
    Json::Object(item)
}

fn policy_from_json(policy: &Json) -> LifecyclePolicy {
    let mut builder = LifecyclePolicy::builder();
    if let Some(rule) = policy["TransitionToIA"].as_str() {
        builder = builder.transition_to_ia(TransitionToIaRules::from(rule));
    }
    if let Some(rule) = policy["TransitionToPrimaryStorageClass"].as_str() {
        builder = builder
            .transition_to_primary_storage_class(TransitionToPrimaryStorageClassRules::from(rule));
    }
    builder.build()
}

impl EfsApi for AwsEfs<'_> {
    async fn find(&self, region: &str, name: &str) -> Result<Option<FileSystem>> {
        let response = self
            .client(region)
            .describe_file_systems()
            .send()
            .await
            .map_err(|e| sdk_error("efs describe_file_systems", &e))?;
        if response.next_marker().is_some() {
            bail!("Your efs number is over 100. Please implement here.");
        }
        Ok(response
            .file_systems()
            .iter()
            .find(|fs| {
                fs.tags()
                    .iter()
                    .any(|tag| tag.key() == "Name" && tag.value() == name)
            })
            .map(|fs| FileSystem {
                id: fs.file_system_id().to_string(),
                life_cycle_state: fs.life_cycle_state().as_str().to_string(),
            }))
    }

    async fn create(&self, region: &str, args: &Json) -> Result<String> {
        let mut request = self
            .client(region)
            .create_file_system()
            .set_creation_token(args["CreationToken"].as_str().map(str::to_string))
            .set_encrypted(args["Encrypted"].as_bool())
            .set_backup(args["Backup"].as_bool());
        for tag in args["Tags"].as_array().into_iter().flatten() {
            let tag = Tag::builder()
                .set_key(tag["Key"].as_str().map(str::to_string))
                .set_value(tag["Value"].as_str().map(str::to_string))
                .build()
                .map_err(|e| anyhow!("EFS のタグを組めません: {e}"))?;
            request = request.tags(tag);
        }
        let created = request
            .send()
            .await
            .map_err(|e| sdk_error("efs create_file_system", &e))?;
        Ok(created.file_system_id().to_string())
    }

    async fn lifecycle_policies(&self, region: &str, id: &str) -> Result<Json> {
        let response = self
            .client(region)
            .describe_lifecycle_configuration()
            .file_system_id(id)
            .send()
            .await
            .map_err(|e| sdk_error("efs describe_lifecycle_configuration", &e))?;
        Ok(Json::Array(
            response
                .lifecycle_policies()
                .iter()
                .map(policy_json)
                .collect(),
        ))
    }

    async fn put_lifecycle_policies(&self, region: &str, id: &str, policies: &Json) -> Result<()> {
        let policies: Vec<LifecyclePolicy> = policies
            .as_array()
            .into_iter()
            .flatten()
            .map(policy_from_json)
            .collect();
        self.client(region)
            .put_lifecycle_configuration()
            .file_system_id(id)
            .set_lifecycle_policies(Some(policies))
            .send()
            .await
            .map_err(|e| sdk_error("efs put_lifecycle_configuration", &e))?;
        Ok(())
    }

    async fn delete(&self, region: &str, id: &str) -> Result<()> {
        self.client(region)
            .delete_file_system()
            .file_system_id(id)
            .send()
            .await
            .map_err(|e| sdk_error("efs delete_file_system", &e))?;
        Ok(())
    }

    async fn mount_target_network_interface(
        &self,
        region: &str,
        mount_target_id: &str,
    ) -> Result<Option<String>> {
        let response = self
            .client(region)
            .describe_mount_targets()
            .mount_target_id(mount_target_id)
            .send()
            .await
            .map_err(|e| sdk_error("efs describe_mount_targets", &e))?;
        Ok(response
            .mount_targets()
            .first()
            .and_then(|target| target.network_interface_id())
            .map(str::to_string))
    }
}

#[cfg(test)]
pub mod fake {
    use std::cell::RefCell;

    use super::*;

    /// file system 1 つまでを持つ fake。作成直後は lifecycle policy が空
    #[derive(Default)]
    pub struct FakeEfs {
        pub file_system: RefCell<Option<(String, FileSystem, Json)>>,
        /// mount target の id → ENI の id
        pub mount_targets: Vec<(String, String)>,
        pub calls: RefCell<Vec<String>>,
    }

    impl FakeEfs {
        pub fn with(name: &str, state: &str, policies: Json) -> Self {
            let fs = FileSystem {
                id: "fs-fake".to_string(),
                life_cycle_state: state.to_string(),
            };
            Self {
                file_system: RefCell::new(Some((name.to_string(), fs, policies))),
                ..Default::default()
            }
        }

        pub fn calls(&self) -> Vec<String> {
            self.calls.borrow().clone()
        }
    }

    impl EfsApi for FakeEfs {
        async fn find(&self, _region: &str, name: &str) -> Result<Option<FileSystem>> {
            let stored = self.file_system.borrow();
            Ok(stored
                .as_ref()
                .filter(|(stored_name, _, _)| stored_name == name)
                .map(|(_, fs, _)| fs.clone()))
        }

        async fn create(&self, _region: &str, args: &Json) -> Result<String> {
            let name = args["Tags"][0]["Value"].as_str().unwrap_or_default();
            self.calls.borrow_mut().push(format!("create {name}"));
            let fs = FileSystem {
                id: "fs-new".to_string(),
                life_cycle_state: "available".to_string(),
            };
            *self.file_system.borrow_mut() = Some((name.to_string(), fs, json!([])));
            Ok("fs-new".to_string())
        }

        async fn lifecycle_policies(&self, _region: &str, _id: &str) -> Result<Json> {
            let stored = self.file_system.borrow();
            Ok(stored.as_ref().map_or(json!([]), |(_, _, p)| p.clone()))
        }

        async fn put_lifecycle_policies(
            &self,
            _region: &str,
            id: &str,
            policies: &Json,
        ) -> Result<()> {
            self.calls.borrow_mut().push(format!("put_lifecycle {id}"));
            if let Some(stored) = self.file_system.borrow_mut().as_mut() {
                stored.2 = policies.clone();
            }
            Ok(())
        }

        async fn delete(&self, _region: &str, id: &str) -> Result<()> {
            self.calls.borrow_mut().push(format!("delete {id}"));
            *self.file_system.borrow_mut() = None;
            Ok(())
        }

        async fn mount_target_network_interface(
            &self,
            _region: &str,
            mount_target_id: &str,
        ) -> Result<Option<String>> {
            Ok(self
                .mount_targets
                .iter()
                .find(|(id, _)| id == mount_target_id)
                .map(|(_, eni)| eni.clone()))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::fake::FakeEfs;
    use super::*;
    use crate::deploy::poll::testing::{fast, run};
    use crate::stack::testing::{context, read_json, repo_root, vector_dirs};

    fn efs_context() -> EfsContext {
        EfsContext {
            local_mount_path: "/mnt/efs".to_string(),
            access_point_path: "/app".to_string(),
            name: "main-testprj".to_string(),
            region: "ap-northeast-1".to_string(),
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
            let documents = read_json(&path);
            let ctx = context(&root, &read_json(&dir.join("manifest.json")));
            for c in ctx.container.values() {
                let Some(efs) = c.vpc.as_ref().and_then(|vpc| vpc.efs.as_ref()) else {
                    continue;
                };
                let want = &documents["efs"][&efs.name];
                assert_eq!(
                    create_args(efs),
                    want["create_file_system"],
                    "{}",
                    dir.display()
                );
                assert_eq!(lifecycle_policies_should_be(), want["lifecycle_policies"]);
                count += 1;
            }
        }
        assert!(count > 3, "vector が少なすぎます: {count}");
    }

    #[test]
    fn test_ensure_exists_creates_then_sets_lifecycle() {
        let efs = efs_context();
        let api = FakeEfs::default();
        assert_eq!(run(status(&api, &efs)).unwrap(), ResourceStatus::NoExist);
        run(ensure_exists(&api, &efs, fast(3))).expect("create");
        assert_eq!(api.calls(), ["create main-testprj", "put_lifecycle fs-new"]);
        assert_eq!(run(status(&api, &efs)).unwrap(), ResourceStatus::Completed);
        // 在れば何もしない
        run(ensure_exists(&api, &efs, fast(3))).expect("noop");
        assert_eq!(api.calls().len(), 2);
    }

    #[test]
    fn test_status_and_delete() {
        let efs = efs_context();
        let api = FakeEfs::with(&efs.name, "creating", json!([]));
        assert_eq!(run(status(&api, &efs)).unwrap(), ResourceStatus::Progress);
        let api = FakeEfs::with(&efs.name, "available", json!([]));
        assert_eq!(
            run(status(&api, &efs)).unwrap(),
            ResourceStatus::RequireUpdate
        );
        assert!(run(delete_if_exists(&api, &efs)).unwrap());
        assert!(!run(delete_if_exists(&api, &efs)).unwrap());
        assert_eq!(api.calls(), ["delete fs-fake"]);
    }

    #[test]
    fn test_create_times_out_when_file_system_stays_in_progress() {
        struct Stuck(FakeEfs);
        impl EfsApi for Stuck {
            async fn find(&self, _region: &str, _name: &str) -> Result<Option<FileSystem>> {
                Ok(Some(FileSystem {
                    id: "fs-new".to_string(),
                    life_cycle_state: "creating".to_string(),
                }))
            }
            async fn create(&self, region: &str, args: &Json) -> Result<String> {
                self.0.create(region, args).await
            }
            async fn lifecycle_policies(&self, region: &str, id: &str) -> Result<Json> {
                self.0.lifecycle_policies(region, id).await
            }
            async fn put_lifecycle_policies(&self, r: &str, id: &str, p: &Json) -> Result<()> {
                self.0.put_lifecycle_policies(r, id, p).await
            }
            async fn delete(&self, region: &str, id: &str) -> Result<()> {
                self.0.delete(region, id).await
            }
            async fn mount_target_network_interface(
                &self,
                _region: &str,
                _id: &str,
            ) -> Result<Option<String>> {
                Ok(None)
            }
        }
        let api = Stuck(FakeEfs::default());
        let error = run(create(&api, &efs_context(), fast(2))).expect_err("timeout");
        assert!(error.to_string().starts_with("EFS status did not become"));
        assert_eq!(api.0.calls(), ["create main-testprj"]);
    }
}
