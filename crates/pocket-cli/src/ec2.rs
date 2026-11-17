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

#[cfg(not(feature = "ec2"))]
fn unavailable(operation: &str) -> anyhow::Error {
    anyhow::anyhow!(
        "この pocket-rs は EC2 の API 抜き (--no-default-features) でビルドされているため、\
         ec2 {operation} を呼べません。feature `ec2` を付けてビルドしたバイナリを使ってください"
    )
}
