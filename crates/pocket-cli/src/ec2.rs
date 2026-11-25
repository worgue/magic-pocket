//! EC2 の API 呼び出し。
//!
//! aws-sdk-ec2 はコンパイルに約 3GB を使うので feature `ec2` (既定で有効) の後ろに置いて
//! いる。メモリ上限の小さい開発用 VM では `--no-default-features` でビルドし、その
//! バイナリは EC2 を呼ぶ箇所で案内付きのエラーになる。EC2 を呼ぶコードはこの module に
//! 集め、feature あり・なしの両方に同じ関数を置く (片方だけだと、もう片方のビルドが
//! 通らない。feature ありのビルドは CI が確かめる)。

use anyhow::Result;
use aws_config::SdkConfig;

/// `Name` タグが `name` の security group の id。無ければ `None`。
#[cfg(feature = "ec2")]
pub async fn security_group_id(config: &SdkConfig, name: &str) -> Result<Option<String>> {
    let groups = aws_sdk_ec2::Client::new(config)
        .describe_security_groups()
        .filters(
            aws_sdk_ec2::types::Filter::builder()
                .name("tag:Name")
                .values(name)
                .build(),
        )
        .send()
        .await
        .map_err(|e| crate::aws_util::sdk_error("ec2 describe_security_groups", &e))?;
    Ok(groups
        .security_groups()
        .first()
        .and_then(|group| group.group_id())
        .map(str::to_string))
}

#[cfg(not(feature = "ec2"))]
pub async fn security_group_id(_config: &SdkConfig, _name: &str) -> Result<Option<String>> {
    Err(unavailable("describe_security_groups"))
}

/// security group を作る (`Name` タグ付き)。戻り値は group の id
#[cfg(feature = "ec2")]
pub async fn create_security_group(
    config: &SdkConfig,
    name: &str,
    description: &str,
    vpc_id: &str,
) -> Result<String> {
    use aws_sdk_ec2::types::{ResourceType, Tag, TagSpecification};
    let created = aws_sdk_ec2::Client::new(config)
        .create_security_group()
        .group_name(name)
        .description(description)
        .vpc_id(vpc_id)
        .tag_specifications(
            TagSpecification::builder()
                .resource_type(ResourceType::SecurityGroup)
                .tags(Tag::builder().key("Name").value(name).build())
                .build(),
        )
        .send()
        .await
        .map_err(|e| crate::aws_util::sdk_error("ec2 create_security_group", &e))?;
    created
        .group_id()
        .map(str::to_string)
        .ok_or_else(|| anyhow::anyhow!("ec2 create_security_group が group id を返しませんでした"))
}

#[cfg(not(feature = "ec2"))]
pub async fn create_security_group(
    _config: &SdkConfig,
    _name: &str,
    _description: &str,
    _vpc_id: &str,
) -> Result<String> {
    Err(unavailable("create_security_group"))
}

#[cfg(feature = "ec2")]
pub async fn delete_security_group(config: &SdkConfig, group_id: &str) -> Result<()> {
    aws_sdk_ec2::Client::new(config)
        .delete_security_group()
        .group_id(group_id)
        .send()
        .await
        .map_err(|e| crate::aws_util::sdk_error("ec2 delete_security_group", &e))?;
    Ok(())
}

#[cfg(not(feature = "ec2"))]
pub async fn delete_security_group(_config: &SdkConfig, _group_id: &str) -> Result<()> {
    Err(unavailable("delete_security_group"))
}

/// VPC の中にあるもの (VPC を消す前の依存の調査に使う)
#[derive(Debug, Clone, Default, PartialEq)]
pub struct VpcContents {
    pub network_interfaces: Vec<NetworkInterface>,
    pub security_groups: Vec<SecurityGroup>,
    pub nat_gateways: Vec<NatGateway>,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct NetworkInterface {
    pub id: String,
    pub interface_type: Option<String>,
    pub status: Option<String>,
    pub subnet_id: Option<String>,
    pub description: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct SecurityGroup {
    pub id: String,
    pub name: String,
    pub description: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct NatGateway {
    pub id: String,
    pub network_interface_ids: Vec<String>,
}

/// `vpc_ids` の VPC にある ENI・security group・NAT gateway (全ページ)
#[cfg(feature = "ec2")]
pub async fn vpc_contents(config: &SdkConfig, vpc_ids: &[String]) -> Result<VpcContents> {
    let client = aws_sdk_ec2::Client::new(config);
    let filter = aws_sdk_ec2::types::Filter::builder()
        .name("vpc-id")
        .set_values(Some(vpc_ids.to_vec()))
        .build();
    let mut contents = VpcContents::default();
    let mut pages = client
        .describe_network_interfaces()
        .filters(filter.clone())
        .into_paginator()
        .send();
    while let Some(page) = pages.next().await {
        let page =
            page.map_err(|e| crate::aws_util::sdk_error("ec2 describe_network_interfaces", &e))?;
        for eni in page.network_interfaces() {
            contents.network_interfaces.push(NetworkInterface {
                id: eni.network_interface_id().unwrap_or_default().to_string(),
                interface_type: eni.interface_type().map(|t| t.as_str().to_string()),
                status: eni.status().map(|s| s.as_str().to_string()),
                subnet_id: eni.subnet_id().map(str::to_string),
                description: eni.description().map(str::to_string),
            });
        }
    }
    let mut pages = client
        .describe_security_groups()
        .filters(filter.clone())
        .into_paginator()
        .send();
    while let Some(page) = pages.next().await {
        let page =
            page.map_err(|e| crate::aws_util::sdk_error("ec2 describe_security_groups", &e))?;
        for group in page.security_groups() {
            contents.security_groups.push(SecurityGroup {
                id: group.group_id().unwrap_or_default().to_string(),
                name: group.group_name().unwrap_or_default().to_string(),
                description: group.description().map(str::to_string),
            });
        }
    }
    let mut pages = client
        .describe_nat_gateways()
        .filter(filter)
        .into_paginator()
        .send();
    while let Some(page) = pages.next().await {
        let page = page.map_err(|e| crate::aws_util::sdk_error("ec2 describe_nat_gateways", &e))?;
        for nat in page.nat_gateways() {
            contents.nat_gateways.push(NatGateway {
                id: nat.nat_gateway_id().unwrap_or_default().to_string(),
                network_interface_ids: nat
                    .nat_gateway_addresses()
                    .iter()
                    .filter_map(|address| address.network_interface_id())
                    .map(str::to_string)
                    .collect(),
            });
        }
    }
    Ok(contents)
}

#[cfg(not(feature = "ec2"))]
pub async fn vpc_contents(_config: &SdkConfig, _vpc_ids: &[String]) -> Result<VpcContents> {
    Err(unavailable("describe_network_interfaces"))
}

#[cfg(not(feature = "ec2"))]
fn unavailable(operation: &str) -> anyhow::Error {
    anyhow::anyhow!(
        "この pocket は EC2 の API 抜き (--no-default-features) でビルドされているため、\
         ec2 {operation} を呼べません。feature `ec2` を付けてビルドしたバイナリを使ってください"
    )
}
