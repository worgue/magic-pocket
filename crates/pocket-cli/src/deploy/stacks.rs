//! region をまたぐ stack の操作 (deploy の手順が使う)。
//!
//! 手順の順序と分岐を fake でテストできるよう、CloudFormation の呼び出しを trait の裏に置く。

use anyhow::Result;
use aws_config::SdkConfig;

use crate::stack::ops::{
    wait_status, CfnClient, ResourceStatus, StackApi, StackDescription, StackResource, WaitOptions,
};
use crate::stack::StackTemplate;

pub trait Stacks {
    async fn describe(&self, region: &str, name: &str) -> Result<Option<StackDescription>>;
    async fn resources(&self, region: &str, name: &str) -> Result<Vec<StackResource>>;
    async fn create(&self, stack: &StackTemplate) -> Result<()>;
    async fn update(&self, stack: &StackTemplate) -> Result<()>;
    async fn delete(&self, region: &str, name: &str) -> Result<()>;
}

/// region を固定した読み取り (`wait_status` に渡す)
struct InRegion<'a, K> {
    stacks: &'a K,
    region: &'a str,
}

impl<K: Stacks> StackApi for InRegion<'_, K> {
    async fn describe(&self, name: &str) -> Result<Option<StackDescription>> {
        self.stacks.describe(self.region, name).await
    }

    async fn resources(&self, name: &str) -> Result<Vec<StackResource>> {
        self.stacks.resources(self.region, name).await
    }
}

/// stack が `target` になるまで待つ
pub async fn wait(
    stacks: &impl Stacks,
    region: &str,
    name: &str,
    label: &str,
    target: ResourceStatus,
    options: WaitOptions,
) -> Result<()> {
    let api = InRegion { stacks, region };
    wait_status(&api, name, label, target, options).await
}

/// AWS に対する [`Stacks`]
pub struct AwsStacks<'a> {
    pub sdk: &'a SdkConfig,
}

impl Stacks for AwsStacks<'_> {
    async fn describe(&self, region: &str, name: &str) -> Result<Option<StackDescription>> {
        CfnClient::new(self.sdk, region).describe(name).await
    }

    async fn resources(&self, region: &str, name: &str) -> Result<Vec<StackResource>> {
        CfnClient::new(self.sdk, region).resources(name).await
    }

    async fn create(&self, stack: &StackTemplate) -> Result<()> {
        CfnClient::new(self.sdk, &stack.region).create(stack).await
    }

    async fn update(&self, stack: &StackTemplate) -> Result<()> {
        CfnClient::new(self.sdk, &stack.region).update(stack).await
    }

    async fn delete(&self, region: &str, name: &str) -> Result<()> {
        CfnClient::new(self.sdk, region).delete(name).await
    }
}

#[cfg(test)]
pub mod fake {
    //! 呼び出しを記録し、決めた状態を返す [`Stacks`]

    use std::cell::RefCell;
    use std::collections::BTreeMap;

    use super::*;
    use crate::stack::ops::{StackOutput, TEMPLATE_HASH_TAG};

    #[derive(Default)]
    pub struct FakeStacks {
        /// stack 名 → 今の状態 (無ければ stack が無い)
        pub stacks: RefCell<BTreeMap<String, StackDescription>>,
        /// create / update の後に付ける output (stack 名 → output)
        pub outputs: RefCell<BTreeMap<String, Vec<(String, String)>>>,
        /// create / update / delete の後に遷移する状態 (既定は成功)
        pub after_write: RefCell<Option<String>>,
        /// `resources` が返す stack の resource
        pub resources: RefCell<Vec<StackResource>>,
        pub calls: RefCell<Vec<String>>,
    }

    impl FakeStacks {
        pub fn description(status: &str, hash: &str, outputs: &[(&str, &str)]) -> StackDescription {
            StackDescription {
                stack_id: "arn:aws:cloudformation:fake".to_string(),
                status: status.to_string(),
                status_reason: None,
                outputs: outputs
                    .iter()
                    .map(|(key, value)| StackOutput {
                        key: key.to_string(),
                        value: value.to_string(),
                        export_name: None,
                    })
                    .collect(),
                tags: vec![(TEMPLATE_HASH_TAG.to_string(), hash.to_string())],
            }
        }

        /// deploy 済み (template が一致) の stack を置く
        pub fn put_synced(&self, stack: &StackTemplate, outputs: &[(&str, &str)]) {
            self.put(
                &stack.name,
                Self::description("UPDATE_COMPLETE", &stack.template_hash(), outputs),
            );
        }

        pub fn put(&self, name: &str, description: StackDescription) {
            self.stacks
                .borrow_mut()
                .insert(name.to_string(), description);
        }

        pub fn calls(&self) -> Vec<String> {
            self.calls.borrow().clone()
        }

        /// 読み取り (describe / resources) を除いた呼び出し
        pub fn writes(&self) -> Vec<String> {
            self.calls()
                .into_iter()
                .filter(|call| !call.starts_with("describe ") && !call.starts_with("resources "))
                .collect()
        }

        fn written(&self, stack: &StackTemplate, default_status: &str) {
            let status = self
                .after_write
                .borrow()
                .clone()
                .unwrap_or_else(|| default_status.to_string());
            let outputs = self
                .outputs
                .borrow()
                .get(&stack.name)
                .cloned()
                .unwrap_or_default();
            let pairs: Vec<(&str, &str)> = outputs
                .iter()
                .map(|(k, v)| (k.as_str(), v.as_str()))
                .collect();
            self.put(
                &stack.name,
                Self::description(&status, &stack.template_hash(), &pairs),
            );
        }
    }

    impl Stacks for FakeStacks {
        async fn describe(&self, region: &str, name: &str) -> Result<Option<StackDescription>> {
            self.calls
                .borrow_mut()
                .push(format!("describe {region} {name}"));
            Ok(self.stacks.borrow().get(name).cloned())
        }

        async fn resources(&self, region: &str, name: &str) -> Result<Vec<StackResource>> {
            self.calls
                .borrow_mut()
                .push(format!("resources {region} {name}"));
            Ok(self.resources.borrow().clone())
        }

        async fn create(&self, stack: &StackTemplate) -> Result<()> {
            self.calls
                .borrow_mut()
                .push(format!("create {} {}", stack.region, stack.name));
            self.written(stack, "CREATE_COMPLETE");
            Ok(())
        }

        async fn update(&self, stack: &StackTemplate) -> Result<()> {
            self.calls
                .borrow_mut()
                .push(format!("update {} {}", stack.region, stack.name));
            self.written(stack, "UPDATE_COMPLETE");
            Ok(())
        }

        async fn delete(&self, region: &str, name: &str) -> Result<()> {
            self.calls
                .borrow_mut()
                .push(format!("delete {region} {name}"));
            match self.after_write.borrow().clone() {
                Some(status) => {
                    if let Some(found) = self.stacks.borrow_mut().get_mut(name) {
                        found.status = status;
                    }
                }
                None => {
                    self.stacks.borrow_mut().remove(name);
                }
            }
            Ok(())
        }
    }
}
